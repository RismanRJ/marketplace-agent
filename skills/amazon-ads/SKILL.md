---
name: amazon-ads
description: Pull Amazon Sponsored Products telemetry (Ads API reporting v3), compute ACoS/RoAS/CTR, and propose keyword bid and campaign budget changes against config-driven thresholds. Use this for Amazon ad performance analysis, ACoS/RoAS reporting, "which keywords are underperforming", bid or budget optimisation on Amazon Ads, or A/B evaluation of Amazon listing images against CTR. Never applies a change itself — every proposal goes through the bid-reviewer agent first.
allowed-tools: Bash, Read, Write, Agent
---

# Amazon Ads

This skill is **transport-agnostic**: Amazon's official Ads MCP server (remote HTTP, open beta) when it is connected, `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-ads ...` from Bash when it is not. Policy — the decision layer, thresholds, caps, cooldowns, HALT, and the reviewer gate — is identical either way. Only the mechanics differ, and those differences are called out below wherever they apply.

## 1. Choose the transport, before any operation

Check the available tool list for tools matching the Ads MCP naming convention `<tool_group>-<tool_name>` for one of the four known groups: `account_management`, `billing`, `campaign_management`, `reporting`.

- **MCP tools present → MCP is primary.** *** Their exact names are not published *** — the only confirmed real example is `account_management-create_advertiser_account`; everything else must be discovered at runtime. For each operation below, find the tool by matching its group to the job (`reporting` for telemetry pulls, `campaign_management` for bid/budget writes, `account_management` for profile/account lookups) and inspect its schema before calling it. If nothing in the list clearly does the operation, **stop and record a blocker** — never guess a tool name, and never invent parameters it didn't declare.
- **MCP tools absent → fall back to `mp_api.py`** exactly as documented in the rest of this file.

Every guardrail in this skill — thresholds, cooldown, `min_impressions`, caps, the reviewer verdict, the audit record — applies identically on both transports. Only §7 (apply) and §8 (record) mechanically differ; read those carefully before acting.

## 2. Transport and auth

Both SP-API and Ads API authenticate with an LWA bearer token (refresh_token exchange, cached with expiry, refreshed by `mp_api.py`). On the `mp_api.py` path, Ads API calls additionally need `Authorization: Bearer <LWA token>`, `Amazon-Advertising-API-ClientId: <client id>`, and `Amazon-Advertising-API-Scope: <profileId>` — the advertising profile id, **one per marketplace/region**, not one per account. Get the right profile from the config entry's `marketplace_id`; don't assume one profile covers every SKU.

On the **MCP path**, auth is handled by the connection itself (OAuth, already established) — do not pass bearer tokens or client ids yourself. Instead, identifiers go in the **request body as parameters** on the discovered tool call — `profileId`, `managerAccountId`, or `advertiserAccountId` depending on which tool you're calling — never as an `Amazon-Advertising-API-Scope` header, which doesn't apply to MCP. Get the profile id by calling the `account_management` group's listing tool rather than assuming one; the profile is still per marketplace/region on this transport too.

All credentials are environment variables read internally by `mp_api.py`. **Never read, cat, grep, or echo them.** If a call fails on auth, report the credential as missing or invalid — don't go looking for it in `.env`.

## 3. Pulling telemetry

On MCP, telemetry lives in the `reporting` group — discover the tool there and use whatever request/poll shape its schema declares; it may or may not mirror the async flow below. On the `mp_api.py` fallback, Ads reporting v3 is **fully asynchronous** — create, poll, download, there is no synchronous report endpoint:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-ads report --type sp-keyword --days 7 --campaign-ids <ids>
```

Internally: `POST /reporting/reports` with a body like

```json
{
  "name": "sp-targeting-7d",
  "startDate": "2026-09-20",
  "endDate": "2026-09-27",
  "configuration": {
    "adProduct": "SPONSORED_PRODUCTS",
    "groupBy": ["targeting"],
    "columns": ["date", "campaignId", "adGroupId", "keywordId", "keyword", "matchType",
                "impressions", "clicks", "cost", "sales7d", "purchases7d"],
    "reportTypeId": "spTargeting",
    "timeUnit": "SUMMARY",
    "format": "GZIP_JSON"
  }
}
```

There is no dedicated keyword report type — keyword-level data lives under `reportTypeId: spTargeting`. Then `GET /reporting/reports/{reportId}` repeatedly until status is terminal. **UNVERIFIED**: the exact enum spelling is assumed `PENDING -> PROCESSING -> COMPLETED | FAILED` — treat any unrecognized status as "still processing", not a crash. On `COMPLETED`, the response carries a presigned S3 `url`; download it **without** the `Authorization` header (S3 rejects the Ads bearer token), then gunzip to get the JSON rows.

**Column names are v3, never v2**: use `sales7d`, `purchases7d`, `keyword`, `keywordId`, `cost`, `clicks`, `impressions`, `campaignId`, `adGroupId`, `matchType`. The v2 names `attributedSales7d`, `attributedConversions7d`, `keywordText` are stale — if they show up anywhere (cache, an old script), treat that data as untrustworthy and re-pull.

## 4. Computing metrics

Per row: `ACoS % = cost / sales7d * 100`, `RoAS = sales7d / cost`, `CTR % = clicks / impressions * 100`. Handle `sales7d == 0` explicitly — ACoS/RoAS are undefined, not `0` and not a crash. Report those keywords as "no attributed sales" and route them through the clicks-with-no-conversions rule below, never through an ACoS comparison.

## 5. Decision rules

Read `config/skus.json` first — merge `defaults` with the matching SKU's overrides. Never write a literal number; cite the key:

- `target_acos_pct` — ACoS a keyword/campaign should sit at or under
- `min_impressions` — minimum sample before an entity is eligible for ANY change
- `max_bid_change_pct` / `max_budget_change_pct` — cap on one change's magnitude
- `bid_floor` / `bid_ceiling` — hard bounds a proposed bid must stay inside
- `cooldown_hours` — minimum time since the entity's last change
- `max_changes_per_run` — cap on total changes proposed in one run

Rule shapes, applied only at or above `min_impressions`:

- **Underperformer → decrease.** ACoS above `target_acos_pct` by a meaningful margin, or clicks with `sales7d == 0`. Decrease capped at `max_bid_change_pct`, floored at `bid_floor`.
- **Strong performer → increase.** ACoS comfortably under `target_acos_pct` with headroom. Increase capped at `max_bid_change_pct`, ceilinged at `bid_ceiling`.
- **Campaign budget** follows the same shape against `max_budget_change_pct`, `entity_type: campaign_budget`.

Below `min_impressions`, propose nothing — log it as skipped for insufficient sample. Every change carries a `rule` (which shape fired) and a human-readable `reason` (the actual numbers). Before proposing, check cooldown per entity:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" cooldown check --entity <keywordId-or-campaignId> --hours <cooldown_hours>
```

Non-zero exit means it changed too recently — skip it this run.

## 6. Propose, do not apply

Cap the batch at `max_changes_per_run`. Write `analytics/proposals/<run_id>.amazon.json` in the contract's exact schema:

```json
{
  "run_id": "<run_id>",
  "channel": "amazon",
  "proposed_at": "<iso8601>",
  "changes": [
    {
      "entity_type": "keyword",
      "entity_id": "<keywordId>",
      "campaign_id": "<campaignId>",
      "entity_label": "<keyword text>",
      "window_days": 7,
      "observed": {"impressions": 4210, "clicks": 138, "spend": 62.40, "sales": 118.00,
                   "conversions": 9, "acos_pct": 52.9, "roas": 1.89},
      "current_value": 0.85,
      "proposed_value": 0.72,
      "delta_pct": -15.3,
      "rule": "underperformer_high_acos",
      "reason": "ACoS 52.9% vs target 25.0%, 4210 impressions (above min 1000)"
    }
  ]
}
```

Delegate the file to the reviewer and wait — you do not decide, it does:

```
Agent({
  description: "Review Amazon bid proposal",
  subagent_type: "bid-reviewer",
  prompt: "Review analytics/proposals/<run_id>.amazon.json against config/skus.json thresholds.",
  run_in_background: false
})
```

Do not touch any live keyword or campaign until that verdict comes back.

## 7. Apply only reviewed changes

Read `analytics/decisions/bid_reviews.jsonl` for this `run_id`. Apply only `verdict: approve` or `verdict: amend` entries; for `amend` use the reviewer's `approved_value`, never your own `proposed_value`. Drop `reject`ed entries and log why — do not retry them this run.

**On the `mp_api.py` path**, dry-run first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-ads update-bids --file <approved_batch.json> --dry-run
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-ads update-budgets --file <approved_batch.json> --dry-run
```

Drop `--dry-run` only after confirming the printed request matches what you expect. These are `PUT /sp/keywords` (`Content-Type`/`Accept: application/vnd.spKeyword.v3+json`) and `PUT /sp/campaigns` (`application/vnd.spCampaign.v3+json`), both **multi-status**: a 200 overall does not mean every item succeeded — parse the per-item status and treat each failed item as its own outcome.

Chunk batches conservatively at 100 items. **UNVERIFIED**: the exact v3 batch size limit and whether the body wraps the array under a `"keywords"`/`"campaigns"` key or sends it bare — confirm against the OpenAPI spec before assuming either; 100 is a deliberately conservative constant, not a confirmed limit.

**On the MCP path**, there is no `--dry-run` flag on any tool. The reviewer verdict itself is the gate that replaces it: produce the proposal and get `approve`/`amend`/`reject` back **without calling any write tool**, and only then call the discovered `campaign_management` write tool for the approved entries. Never call a write tool "just to see what happens" — on this transport that is not a preview, it is a live mutation.

## 8. Record every applied change

Carry a `before` value (and a `reason`) on every entity in the batch, so `mp_state.py rollback` can compute the inverse mechanically. The two transports diverge here — get this right, it's the difference between a working audit trail and a blind one:

- **`mp_api.py` path**: it writes the audit record itself for every mutating call, keyed on the entity, with your `before` value, only when the vendor actually accepted the write. Do **NOT** run `mp_state.py record` yourself after it — a second record double-counts the change and starts a false cooldown.
- **MCP path**: the MCP tool call does **NOT** write an audit record — it's just a vendor write. You must call it yourself, immediately after every applied change, with the `before` value captured **before** the write:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" record --kind bids --payload <file>
  ```

  (`--kind budgets` for campaign budgets.) Skipping this on the MCP path leaves cooldown and rollback blind — they read only the audit log, not Amazon. There is no way around calling it yourself here.

Full sequence, no exceptions, either transport: `check-halt -> cooldown check -> dry-run -> reviewer verdict -> apply -> record`. On MCP, "dry-run" has no vendor mechanic — the reviewer verdict is what stands in for it, per §7.

## 9. A/B creative evaluation

Correlate the listing patch timestamp (recorded by the image pipeline) with the daily CTR series for the affected SKU/keywords:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-ads report --type sp-keyword --days <window> --time-unit DAILY
```

Use `DAILY` `timeUnit`, not `SUMMARY` — you need the day-by-day split around the patch. Require both `ab_min_impressions_per_variant` and `ab_min_days` (from config) met **per variant** before calling a winner. Below either threshold, report **INCONCLUSIVE** — do not claim a winner or a confidence level you haven't computed. Never pool Amazon and Flipkart data into one comparison.

## 10. Throttling

Ads API uses a token bucket per endpoint. On 429, honour `Retry-After` if present; otherwise `mp_api.py` backs off exponentially with jitter — this is handled inside `mp_api.py`, not reimplemented here. The Ads MCP server's rate limits are **not documented at all** (open beta) — treat every MCP write as potentially throttled, back off on any error that looks like a rate limit, and never assume it's safe to fire a large batch quickly. If a call still fails after retries on either transport, degrade the Amazon channel for this run and report why; do not abort the whole run over one throttled endpoint.

## 11. Beta boundaries on the MCP path

The Ads MCP server is in **open beta**: tool names are undocumented and must be discovered per §1, rate limits are undocumented (see §10), and the server exposes destructive writes — including campaign deletion — that this skill must never touch. This skill only adjusts bids and budgets within the caps in §5; it never calls a delete, archive, or terminate tool, and campaign creation is likewise outside the autonomy boundary regardless of transport. If the only tool you can find for an operation looks like it creates or deletes a campaign, that is not this skill's job — stop and record a blocker instead of using it.
