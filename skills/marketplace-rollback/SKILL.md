---
name: marketplace-rollback
description: Undo bid, budget, price, or image changes a previous marketplace-agent run applied — use when metrics degraded sharply after a run, a change turned out wrong, or an operator explicitly asks to revert. Generates the inverse change set from the audit log, routes it back through the same reviewer as a forward change, then applies and re-audits it.
allowed-tools: Bash, Read
---

# Marketplace Rollback

A rollback is a live mutation, not an undo button. Treat it with the same caution as the run that
made the original change — it goes through the same reviewer, the same caps, the same audit trail.

## 1. When to use this

- A run applied bid/budget/price/image changes that turned out wrong.
- Metrics (ACOS, spend, CTR) degraded sharply right after a run.
- An operator asks to undo a specific run's changes.

## 2. If this is broad or urgent, HALT first

If the situation is bad enough that you need a broad rollback (multiple channels, multiple runs, or
"undo everything from today"), write the HALT file **before anything else in this skill** — not as a
last step:

```bash
echo "rollback in progress: <reason>" > "${CLAUDE_PROJECT_DIR}/HALT"
```

This stops any scheduled `marketplace-run` from re-applying the same changes while you are still
undoing them. For a single narrow rollback (one SKU, one bid) you may skip this, but default to
writing it if in doubt.

## 3. Find the run

List recent run ids from the audit log:

```bash
ls -t analytics/audit/*.jsonl
tail -n 50 analytics/audit/<kind>.jsonl | python3 -c "import sys,json; [print(json.loads(l)['run_id'], json.loads(l)['ts'], json.loads(l)['entity_id']) for l in sys.stdin]"
```

Confirm the `run_id` and `kind` (the real kinds written by this plugin are `bids`, `budgets`, `listing_patches`, `price`, `inventory`; a kind with no audit file yields an EMPTY change set and a silent no-op, so get it right) the operator means
before generating anything.

## 4. Generate the inverse change set

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" rollback --kind <kind> --run-id <run_id>
```

This **emits a proposal only** — it applies nothing. `--dry-run` entries in the original audit log are
excluded automatically, since nothing was actually sent for those and there is nothing to invert.

## 5. Review the inverse set

Route the emitted proposal through `@agent-marketplace-agent:bid-reviewer`, exactly like a forward
change — it independently recomputes, and enforces caps, floor/ceiling, and cooldown. A rollback can
itself breach a cap or a floor/ceiling just as easily as the original change did.

One asymmetry, handled in the data rather than by an override: set `"rule": "rollback"` on every
change in the inverse set. The reviewer skips the cooldown check for those, because the cooldown
exists to stop a value drifting further on every run, and restoring a previously-live value is the
opposite of drift. Every other check — caps, floor/ceiling, direction sanity, arithmetic — still
applies in full. There is no operator override flag anywhere in this plugin, and nothing may reach
`apply` without an `approve` or `amend` verdict.

## 6. Apply and record

The full mutation sequence applies to a rollback too — it is a live mutation, not a special case:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" check-halt
```

If you wrote `HALT` in step 1, remove it only once you are ready to apply the revert, and put it back if anything goes wrong mid-way.

Apply the reviewed inverse set through the relevant channel skill (`amazon-ads`, `amazon-sp`,
`flipkart-ads`, `flipkart-sp` — whichever owns that `kind`). Then record each reverted change:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" record --kind <kind> --payload <file>
```

The rollback must be auditable and itself reversible, same as any other mutation.

## 7. Image rollback — one extra check

Revert the catalog image attribute to the previous `media_location` captured in the original audit
line's `before` field.

Before applying: confirm that previous image URL is still publicly reachable (fetch it, don't assume).
If the old asset was taken down or the hosting expired, rollback of that image **cannot succeed** —
report it as a blocker, do not silently skip it, and do not substitute a different image on your own
judgement.

## 8. Release HALT

If you wrote the HALT file in step 2, remove it once the rollback is applied and verified — otherwise
every future scheduled run stays blocked indefinitely.

```bash
rm -f "${CLAUDE_PROJECT_DIR}/HALT"
```
