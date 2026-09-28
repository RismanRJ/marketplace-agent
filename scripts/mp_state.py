#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK_STALE_SECONDS = 2 * 3600


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


DECISION_KINDS = {
    "image_approval": "image_approvals.jsonl",
    "bid_review": "bid_reviews.jsonl",
}


def _audit_dir(root=None) -> Path:
    return (root or project_root()) / "analytics" / "audit"


def _record_path(kind, root=None) -> Path:
    """Decision-layer verdicts go to analytics/decisions/ where the gates read them;
    everything else is an ordinary audit record."""
    if kind in DECISION_KINDS:
        return (root or project_root()) / "analytics" / "decisions" / DECISION_KINDS[kind]
    return _audit_dir(root) / f"{kind}.jsonl"


def _lock_dir(root=None) -> Path:
    return (root or project_root()) / ".mp-locks"


def _lock_path(name, root=None) -> Path:
    return _lock_dir(root) / f"{name}.lock"


def _halt_path(root=None) -> Path:
    return (root or project_root()) / "HALT"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


run_id = _new_run_id


def halted(root=None) -> bool:
    return _halt_path(root).exists()


def _iter_audit_entries(kind=None, root=None):
    adir = _audit_dir(root)
    if not adir.exists():
        return
    files = [adir / f"{kind}.jsonl"] if kind else sorted(adir.glob("*.jsonl"))
    for f in files:
        if not f.exists():
            continue
        with f.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def last_change(entity_id, root=None):
    best = None
    for entry in _iter_audit_entries(root=root):
        if entry.get("entity_id") != entity_id or entry.get("dry_run"):
            continue
        if best is None or entry.get("ts", "") > best.get("ts", ""):
            best = entry
    return best


def record(*, kind, channel, entity_id, before, after, reason, actor, dry_run,
           run_id=None, root=None):
    entry = {
        "ts": _now_iso(),
        "run_id": run_id or _new_run_id(),
        "kind": kind,
        "channel": channel,
        "entity_id": entity_id,
        "before": before,
        "after": after,
        "reason": reason,
        "actor": actor,
        "dry_run": bool(dry_run),
    }
    path = _record_path(kind, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
    return entry


def cooldown_status(entity_id, hours, root=None):
    change = last_change(entity_id, root=root)
    if change is None:
        return True, None
    try:
        changed_at = datetime.fromisoformat(change["ts"])
    except (KeyError, ValueError):
        return True, None
    age_hours = (datetime.now(timezone.utc) - changed_at).total_seconds() / 3600
    return age_hours >= hours, age_hours


def lock_acquire(name, root=None):
    ldir = _lock_dir(root)
    ldir.mkdir(parents=True, exist_ok=True)
    path = _lock_path(name, root)
    owner = os.getppid()
    payload = {"pid": owner, "ts": _now_iso(), "epoch": time.time()}

    def _create():
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps(payload).encode())
        finally:
            os.close(fd)

    try:
        _create()
        return {"acquired": True, "name": name, "pid": owner, "stolen": False}
    except FileExistsError:
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
        age = time.time() - existing.get("epoch", 0)
        if age > LOCK_STALE_SECONDS:
            print(
                f"lock '{name}' is stale (age {age / 3600:.1f}h, held by pid "
                f"{existing.get('pid')}); stealing it",
                file=sys.stderr,
            )
            path.unlink(missing_ok=True)
            _create()
            return {"acquired": True, "name": name, "pid": owner, "stolen": True}
        return {
            "acquired": False,
            "name": name,
            "held_by_pid": existing.get("pid"),
            "held_since": existing.get("ts"),
        }


def lock_release(name, root=None):
    path = _lock_path(name, root)
    owner = os.getppid()
    if not path.exists():
        print(f"lock '{name}' is not held; nothing to release", file=sys.stderr)
        return {"released": False, "name": name, "reason": "not_held"}
    try:
        existing = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        existing = {}
    if existing.get("pid") != owner:
        print(
            f"lock '{name}' is owned by pid {existing.get('pid')}, not {owner}; "
            f"refusing to release",
            file=sys.stderr,
        )
        return {"released": False, "name": name, "reason": "not_owner",
                 "held_by_pid": existing.get("pid")}
    path.unlink()
    return {"released": True, "name": name}


def build_rollback_changes(kind, target_run_id, root=None):
    entries = [
        e for e in _iter_audit_entries(kind=kind, root=root)
        if e.get("run_id") == target_run_id and not e.get("dry_run")
    ]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    changes = []
    for e in entries:
        changes.append({
            "kind": e.get("kind"),
            "channel": e.get("channel"),
            "entity_id": e.get("entity_id"),
            "revert_to": e.get("before"),
            "revert_from": e.get("after"),
            "original_reason": e.get("reason"),
            "original_ts": e.get("ts"),
        })
    return changes


def _cmd_check_halt(args):
    path = _halt_path()
    if path.exists():
        reason = path.read_text()
        print(json.dumps({"halted": True, "reason": reason}))
        print(f"HALT active: {reason.strip()}", file=sys.stderr)
        return 1
    print(json.dumps({"halted": False}))
    return 0


def _cmd_lock_acquire(args):
    result = lock_acquire(args.name)
    print(json.dumps(result))
    return 0 if result["acquired"] else 1


def _cmd_lock_release(args):
    result = lock_release(args.name)
    print(json.dumps(result))
    return 0 if (result["released"] or result["reason"] == "not_held") else 1


def _cmd_cooldown_check(args):
    clear, age_hours = cooldown_status(args.entity, args.hours)
    result = {"clear": clear, "entity_id": args.entity, "hours_required": args.hours,
               "age_hours": age_hours}
    print(json.dumps(result))
    if not clear:
        print(f"entity '{args.entity}' changed {age_hours:.2f}h ago, "
              f"cooldown is {args.hours}h", file=sys.stderr)
        return 1
    return 0


def _cmd_record(args):
    payload = json.loads(Path(args.payload).read_text())
    entry = record(
        kind=args.kind,
        channel=payload.get("channel"),
        entity_id=payload.get("entity_id"),
        before=payload.get("before"),
        after=payload.get("after"),
        reason=payload.get("reason"),
        actor=payload.get("actor"),
        dry_run=payload.get("dry_run", False),
        run_id=payload.get("run_id"),
    )
    print(json.dumps(entry))
    return 0


def _cmd_rollback(args):
    changes = build_rollback_changes(args.kind, args.run_id)
    result = {
        "proposal": True,
        "run_id": args.run_id,
        "kind": args.kind,
        "changes": changes,
        "note": "PROPOSAL ONLY - nothing was applied. Review and apply manually.",
    }
    print(json.dumps(result))
    print("This is a rollback PROPOSAL only; it was not applied.", file=sys.stderr)
    return 0


def _selfcheck():
    import tempfile

    old_root = os.environ.get("CLAUDE_PROJECT_DIR")
    tmpdir = tempfile.mkdtemp(prefix="mp_state_selfcheck_")
    os.environ["CLAUDE_PROJECT_DIR"] = tmpdir
    try:
        assert halted() is False

        halt_file = _halt_path()
        halt_file.write_text("manual halt for test\n")
        assert halted() is True
        halt_file.unlink()
        assert halted() is False

        r1 = lock_acquire("testlock")
        assert r1["acquired"] is True and r1["stolen"] is False

        r2 = lock_acquire("testlock")
        assert r2["acquired"] is False

        lock_file = _lock_path("testlock")
        payload = json.loads(lock_file.read_text())
        payload["epoch"] = time.time() - (3 * 3600)
        lock_file.write_text(json.dumps(payload))

        r3 = lock_acquire("testlock")
        assert r3["acquired"] is True and r3["stolen"] is True

        r4 = lock_release("testlock")
        assert r4["released"] is True
        assert not lock_file.exists()

        r5 = lock_release("testlock")
        assert r5["released"] is False and r5["reason"] == "not_held"

        entity = "sku-cooldown-test"
        clear, age = cooldown_status(entity, 24)
        assert clear is True and age is None

        record(kind="bids", channel="amazon", entity_id=entity,
               before={"bid": 1.0}, after={"bid": 1.1}, reason="test",
               actor="selfcheck", dry_run=False)

        bids_path = _audit_dir() / "bids.jsonl"
        lines = [json.loads(x) for x in bids_path.read_text().splitlines() if x.strip()]
        lines[-1]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        bids_path.write_text("\n".join(json.dumps(e) for e in lines) + "\n")

        clear_ok, age_ok = cooldown_status(entity, 5)
        assert clear_ok is True and age_ok >= 5

        clear_blocked, age_blocked = cooldown_status(entity, 20)
        assert clear_blocked is False and age_blocked < 20

        rid = run_id()
        e1 = record(kind="bids", channel="amazon", entity_id="sku-a",
                    before={"bid": 1.0}, after={"bid": 1.2}, reason="raise",
                    actor="selfcheck", dry_run=False, run_id=rid)
        e2 = record(kind="bids", channel="amazon", entity_id="sku-b",
                    before={"bid": 2.0}, after={"bid": 1.8}, reason="lower",
                    actor="selfcheck", dry_run=False, run_id=rid)
        record(kind="bids", channel="amazon", entity_id="sku-c",
               before={"bid": 3.0}, after={"bid": 3.5}, reason="dry-run-should-be-excluded",
               actor="selfcheck", dry_run=True, run_id=rid)

        changes = build_rollback_changes("bids", rid)
        assert len(changes) == 2
        assert changes[0]["entity_id"] == "sku-b"
        assert changes[0]["revert_to"] == e2["before"]
        assert changes[0]["revert_from"] == e2["after"]
        assert changes[1]["entity_id"] == "sku-a"
        assert changes[1]["revert_to"] == e1["before"]
        assert changes[1]["revert_from"] == e1["after"]
    finally:
        if old_root is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old_root

    print("selfcheck ok")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="mp_state.py")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check-halt")

    lock_p = sub.add_parser("lock")
    lock_sub = lock_p.add_subparsers(dest="lock_action", required=True)
    la = lock_sub.add_parser("acquire")
    la.add_argument("--name", required=True)
    lr = lock_sub.add_parser("release")
    lr.add_argument("--name", required=True)

    cd_p = sub.add_parser("cooldown")
    cd_sub = cd_p.add_subparsers(dest="cooldown_action", required=True)
    cdc = cd_sub.add_parser("check")
    cdc.add_argument("--entity", required=True)
    cdc.add_argument("--hours", required=True, type=float)

    rec_p = sub.add_parser("record")
    rec_p.add_argument("--kind", required=True)
    rec_p.add_argument("--payload", required=True)

    rb_p = sub.add_parser("rollback")
    rb_p.add_argument("--kind", required=True)
    rb_p.add_argument("--run-id", required=True, dest="run_id")

    sub.add_parser("new-run-id")
    sub.add_parser("selfcheck")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "new-run-id":
        print(json.dumps({"run_id": _new_run_id()}))
        return 0
    if args.command == "check-halt":
        return _cmd_check_halt(args)
    if args.command == "lock":
        return _cmd_lock_acquire(args) if args.lock_action == "acquire" else _cmd_lock_release(args)
    if args.command == "cooldown":
        return _cmd_cooldown_check(args)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "rollback":
        return _cmd_rollback(args)
    if args.command == "selfcheck":
        return _selfcheck()
    return 2


if __name__ == "__main__":
    sys.exit(main())
