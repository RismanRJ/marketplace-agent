---
name: marketplace-run
description: Run one full autonomous marketplace cycle across Amazon and Flipkart — preflight guards, ad telemetry pull, image A/B pipeline, reviewed bid and budget optimisation, A/B evaluation, dashboard, and an honest run report. Use this to run the marketplace agent, run a cycle or a loop, optimise ads across channels, or when scheduled unattended. Also use it to understand how the phases fit together before running an individual channel skill.
argument-hint: "[--dry-run] [--channel amazon|flipkart|both] [--phase telemetry|images|bids|report]"
allowed-tools: Bash, Read, Write, Agent, Skill
---

# Marketplace Run — Orchestration

You are the run controller. You sequence phases, enforce guards, delegate to channel skills and to
the decision layer, and produce one honest report. **You do not call marketplace APIs directly** —
channel skills do that.

Read `CLAUDE.md` at the project root first. It holds the autonomy boundary and the invariants, and
it overrides anything here that has drifted.

## Arguments

- `--dry-run` — run every phase, propose everything, apply nothing. Default when you are unsure.
- `--channel amazon|flipkart|both` (default `both`)
- `--phase ...` — run a single phase instead of the full cycle.

## Phase 0 — Preflight (never skip)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" check-halt || exit 0     # HALT file present: stop, report, do nothing
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" lock acquire --name marketplace-run
```

If the lock is held, **stop**. Another run is live; two runs double-apply bid changes. Report and exit.

Then:
1. Load `config/skus.json`. If missing or unparseable, stop — there is no safe default set of SKUs.
2. Mint a `run_id` and use it on every record this run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" new-run-id
```
3. Check that the credentials each selected channel needs are present as environment variables
   (names are in the plugin README). **Check presence only — never read, echo, or log a value.**
   A channel with missing credentials starts `degraded`, it does not abort the run.
4. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" check-credentials --channel all` — presence
   isn't enough, a refresh token can be dead while the env var is still set. On `expired`, mark that
   channel `degraded` and record a blocker, and continue with the other channel. On `warn` (refresh
   token expiring soon), the channel stays `ok` for this run, but record a blocker anyway so it
   surfaces on the dashboard and in the run report — an operator needs to know a credential is dying
   before it actually does.

Maintain a `channel_state` of `ok | degraded` for each channel. Any hard stop from the autonomy
boundary sets that channel to `degraded` with a reason, and the run continues on the other channel.

## Phase 1 — Telemetry

Both channels are independent; run them concurrently.

- Amazon → `amazon-ads` skill: pull the Sponsored Products report for the configured lookback,
  write to `analytics/amazon_live_pulls/`. Provenance `Live API (SP-API / Ads API)`.
- Flipkart → `flipkart-ads` skill: browser session against the seller portal, write metrics and
  screenshot proofs to `analytics/flipkart_snapshots/`. Provenance `Autonomous Browser Session`.

A CAPTCHA, OTP, or login wall on Flipkart is a hard stop **for Flipkart only**: record a blocker,
mark it degraded, and carry on with Amazon. Do not attempt credential entry or CAPTCHA solving.

## Phase 2 — Image A/B pipeline

Delegate to the `image-pipeline` skill. It generates variants, gates each one through
`@agent-marketplace-agent:image-approver`, and promotes only approved assets to `assets/approved/`.

Then patch catalogs with approved variants only:
- Amazon → `amazon-sp`. Requires the approved asset to already be hosted at a publicly fetchable
  https/s3 URL; Amazon's crawler fetches it. A local path cannot be submitted.
- Flipkart → **no API path exists.** Flipkart listing images cannot be changed through any Seller
  API. Record the approved variant as pending a portal upload (Catalog Manager or the Excel
  bulk-upload flow) and move on. Never report that a Flipkart image was patched via API.

**An image that has no `approved` verdict in `analytics/decisions/image_approvals.jsonl` must never
reach a catalog.** If you cannot find the verdict, treat it as not approved.

Record the patch timestamp against the SKU and variant — Phase 4 needs it to attribute CTR shifts.

## Phase 3 — Bid and budget optimisation

This is the money-moving phase. The sequence is fixed:

**3a. Propose.** The channel ads skill computes proposals from Phase 1 telemetry and writes
`analytics/proposals/<run_id>.<channel>.json` in the bid-proposal schema. Proposing is not applying.

**3b. Review.** Delegate the proposal file to `@agent-marketplace-agent:bid-reviewer`. It
independently recomputes the metrics, enforces caps, floors, ceilings, cooldowns and sample
sufficiency, and returns a per-change verdict.

**3c. Apply only what carries `approve` or `amend`**, using the reviewer's `approved_value` — not
the proposer's. Anything `reject`ed is dropped; log the reason, do not retry it this run.

If the reviewer rejects the whole proposal (a math disagreement does this), apply **nothing** for
that channel and record why. That is a working guardrail, not a failure to route around.

Under `--dry-run`, run 3a and 3b in full and stop before 3c.

## Phase 4 — A/B evaluation

For each SKU with a variant live long enough:
- Require `ab_min_impressions_per_variant` per variant and `ab_min_days` elapsed. Below either
  threshold the result is **inconclusive** — say so and keep both variants running. Do not call a
  winner on thin traffic.
- Compare CTR before and after the patch timestamp, per channel only. Never pool Amazon and
  Flipkart numbers.
- On a conclusive winner, record it. Promoting a winner to a permanent hero image is a catalog
  mutation and goes through Phase 2's rules.

## Phase 5 — Report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" build
```

This writes the offline local copy (`dashboard/unified_view.html`) — always do this, it always works
with no network dependency.

**Shareable dashboard.** Check `config/skus.json`'s `defaults.publish_dashboard`. Absent or `true` →
publish; `false` → skip publishing and stop here (local file only), then release the lock.

> **Data sensitivity.** The dashboard contains seller financial data — ad spend, revenue, ACoS, SKU
> performance. Publishing it uploads that data to claude.ai as a Claude Artifact. If the operator has
> set `"publish_dashboard": false` in `config/skus.json`, do not publish under any circumstance.

When publishing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dashboard.py" build --artifact --out /tmp/dashboard_artifact.html
```

Then, with the Artifact tool:
1. If `analytics/dashboard_url.txt` exists, read its contents (a claude.ai artifact URL) and first
   call `action: "read"` with that `url` — updating an artifact from an earlier session requires
   reading it before you can publish to it.
2. Publish the fragment at `/tmp/dashboard_artifact.html` with `action: "publish"`, `file_path` set
   to that path, `title` "Marketplace Telemetry", `icon` "chart", a one-sentence `description`, and
   `url` set to the value read from `analytics/dashboard_url.txt` if it existed (this updates the
   same artifact in place instead of creating a new one).
3. If `analytics/dashboard_url.txt` did not exist, this publish creates a new artifact — write the
   returned URL to `analytics/dashboard_url.txt` so every later run reuses it and the team's link
   never changes.

State plainly in the report: publishing is what makes the dashboard shareable with the team — a local
HTML file cannot be sent as a link. The artifact is **private by default**; it is shared only if the
user chooses to share the link.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" lock release --name marketplace-run
```

Release the lock even when a phase failed — use a trap or an explicit final step, otherwise the
next scheduled run is locked out by a dead run.

Report must state, plainly:
- run id, mode (live or dry-run), channels attempted and their final state
- what was applied, per channel, with before → after
- what the decision layer **rejected**, and why
- what was skipped for cooldown, cap, or insufficient sample
- every blocker and which channel it degraded
- any A/B result, including inconclusive ones

Do not round a partial run up to a success. If Flipkart was degraded the whole run, the headline is
"Amazon complete, Flipkart degraded — <reason>".

## Failure handling

- Transient API failure (429, 5xx): `mp_api.py` already retries with backoff. The Ads MCP server
  does not — its rate limits are undocumented and it is in beta, so treat a repeated MCP tool
  failure as a degraded channel rather than retrying it in a tight loop. If it still fails,
  degrade that channel and continue.
- Any unexpected exception mid-phase: record a blocker, release the lock, report what had already
  been applied. **Never leave a run half-applied and silent** — a partially applied bid change set
  that nobody knows about is the worst outcome available.
- If something looks wrong enough that continuing could cause harm, write a `HALT` file with the
  reason and stop. A stopped agent is recoverable; a confidently wrong one is not.

## Scheduling

For unattended operation, invoke this skill on a schedule. The lock makes overlapping runs safe,
the cooldowns make frequent runs safe, and `HALT` is the kill switch. Verify a new deployment with
`--dry-run` for at least one full cycle before letting it apply anything.
