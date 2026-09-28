---
name: flipkart-ads
description: Scrape Flipkart Ads Manager (Seller Hub -> Marketing -> Flipkart Ads) via Playwright browser automation for PLA campaign telemetry — clicks, spend, revenue, ROI multiple, CPC bid, daily budget — and propose (never apply directly) bid/budget changes against config-driven thresholds. Use this for Flipkart ad performance analysis or bid/budget optimisation. There is no public Flipkart advertising API; all data here is scraped, not API-grade, and every change goes through the bid-reviewer agent before anything is clicked.
allowed-tools: Bash, Read, Write, Agent, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_type, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_wait_for, mcp__playwright__browser_close
---

# Flipkart Ads

`browser_close` was added to the tool list beyond what was specified up front because step 9 requires it — closing the session is not optional.

## 1. Why this is browser automation

Flipkart publishes no advertising API. Confirmed by exhausting both doc portals (`flipkart.github.io/fk-api-platform-docs` and `seller.flipkart.com/api-docs`) — the entire public surface is Listing Management, Order Management, and Report Management. The Report API's `settled_transaction_ledger` includes PLA spend, but it is a lagging **financial settlement** report, not campaign metrics or bid control. There is no substitute for opening the Ads Manager UI.

Every number this skill produces is scraped from a rendered page, not returned by a documented, versioned API. Label it as such everywhere it's written down or reported: `"source": "Autonomous Browser Session"`. Never present a scraped figure next to `mp_api.py`-sourced Amazon data as if the two were equivalent in reliability or freshness.

## 2. FRAGILITY WARNING — read before touching a selector

The UI structure described anywhere in this repo's notes is secondhand, from third-party guides, not from an authenticated Flipkart session. **No verified DOM selectors exist.** Before trusting any click target:

- The operator should do one live authenticated Playwright pass first to capture the real page structure. Until that's done, treat every selector as a guess.
- Drive every step from a fresh `browser_snapshot` call, never from a selector or `ref` remembered from a previous run or a previous step in this same run. The page can re-render between actions and invalidate refs; a stale `ref` either errors or, worse, silently hits the wrong element.
- `browser_click` and `browser_type` both require a `ref` from a snapshot. There is no "click the Campaigns tab by name" — snapshot first, find the element in that snapshot's output, then act on its `ref`.
- If a snapshot doesn't look like what this skill expects (missing campaign table, different labels, an unexpected interstitial), **stop and record a blocker.** Do not guess at an alternate selector and continue. A silently wrong scrape that feeds a bid change is the worst failure mode in this whole plugin — worse than stopping the run.

## 3. Session verification (hard stop lives here)

```
mcp__playwright__browser_navigate  # to the Seller Hub -> Marketing -> Flipkart Ads URL
mcp__playwright__browser_snapshot
```

Inspect the snapshot for a login form, an OTP prompt, or a CAPTCHA challenge. Per the contract's autonomy boundary, this is a **hard stop for Flipkart only**: never attempt credential entry, never read or enter an OTP, never attempt to solve a CAPTCHA. If any of these appear:

1. Record a blocker (write it alongside this run's other decision records, same `run_id`).
2. Mark the Flipkart channel degraded for this run.
3. Continue the run on the Amazon channel — do not block the whole run waiting on a human.

If the snapshot instead shows the expected campaign dashboard, proceed.

## 4. Telemetry extraction

For each SKU in `config/skus.json` with `"channel": "flipkart"`, using that SKU's `campaign_ids`:

1. Snapshot, find and apply the last-7-days date filter, find the campaign row(s) matching the SKU's campaign ids.
2. Capture: clicks, spend (INR), revenue (INR), the platform's native ROI multiple, current CPC bid, daily budget. Note the campaign type shown (CPC or SmartROI).
3. Re-snapshot after any filter change before reading values — a filter click re-renders the table.

Flipkart natively reports **ROI as a multiple** (`revenue / spend`), i.e. RoAS — it does not report ACoS. Convert for comparison purposes only, and always label the conversion:

```
acos_pct = 100 / roi_multiple
```

Never print a converted Flipkart ACoS next to an Amazon ACoS as if directly comparable — attribution models and windows differ between the two platforms. State that difference whenever both appear in the same report.

Two campaign types matter for what happens next: **CPC** (manual daily budget + CPC bid — editable) and **SmartROI** (algorithm-managed bidding against a target ROI — a manual bid edit on a SmartROI campaign may not apply, or may be silently overridden by the algorithm on the next cycle). Flag SmartROI campaigns as such in the output; don't propose a raw CPC bid change against one without noting the mismatch.

Write:
- Metrics to `analytics/flipkart_snapshots/scraped_ads_metrics.json`, each record tagged `"source": "Autonomous Browser Session"`.
- Screenshots to `analytics/flipkart_snapshots/screenshots/`.

## 5. Screenshot PII warning

A logged-in seller dashboard screenshot contains revenue and account data. Screenshots stay under `analytics/` only:

- Never paste one into a chat report or send it to an external service.
- Never capture a screen showing credentials, an OTP field, or a CAPTCHA challenge — if step 3's hard stop fires, do not screenshot that state either; a blocker record is enough.

## 6. Propose, do not apply

Read `config/skus.json` and merge `defaults` with the matching SKU's overrides — never write a literal threshold number into this skill or into a proposal's `reason` as if it were policy. The keys that govern every decision here: `target_acos_pct`, `min_impressions`, `max_bid_change_pct`, `max_budget_change_pct`, `bid_floor`, `bid_ceiling`, `cooldown_hours`, `max_changes_per_run`. Apply them the same way the Amazon Ads skill does (underperformer above `target_acos_pct` -> decrease capped at `max_bid_change_pct`, floored at `bid_floor`; strong performer well under target with headroom -> increase capped at `max_bid_change_pct`, ceilinged at `bid_ceiling`; below `min_impressions` -> propose nothing, log as skipped for insufficient sample).

Write `analytics/proposals/<run_id>.flipkart.json` in the contract's exact bid proposal schema:

```json
{
  "run_id": "<run_id>",
  "channel": "flipkart",
  "proposed_at": "<iso8601>",
  "changes": [
    {
      "entity_type": "keyword",
      "entity_id": "<flipkart campaign/keyword id>",
      "campaign_id": "<campaign id>",
      "entity_label": "<campaign or keyword name>",
      "window_days": 7,
      "observed": {"impressions": 0, "clicks": 240, "spend": 3100.00, "sales": 5800.00,
                   "conversions": 14, "acos_pct": 53.4, "roas": 1.87},
      "current_value": 12.00,
      "proposed_value": 10.20,
      "delta_pct": -15.0,
      "rule": "underperformer_high_acos",
      "reason": "ROI 1.87x (ACoS ~53.4%, converted) vs target_acos_pct, 240 clicks"
    }
  ]
}
```

`observed.impressions` is frequently unreliable/unset from the scraped campaign table — use `min_impressions` against whatever count the UI actually exposes, and if it doesn't expose one, say so in `reason` rather than inventing a number.

Delegate and wait — this skill never decides for itself:

```
Agent({
  description: "Review Flipkart bid proposal",
  subagent_type: "bid-reviewer",
  prompt: "Review analytics/proposals/<run_id>.flipkart.json against config/skus.json thresholds.",
  run_in_background: false
})
```

Do not touch any live campaign until that verdict comes back.

## 7. Apply only reviewed changes

Read `analytics/decisions/bid_reviews.jsonl` for this `run_id`. Apply only `approve`/`amend` entries, using the reviewer's `approved_value` — never the original `proposed_value`. For each:

1. `browser_snapshot` — find the campaign/keyword row fresh, do not reuse a `ref` from step 4 or 6.
2. Note the pre-edit value from this snapshot as `before` (needed for step 8).
3. `browser_click` the row's inline edit control (by the `ref` just captured), `browser_type` the `approved_value`, save.
4. `browser_snapshot` again and confirm the cell actually shows the new value. `browser_take_screenshot` the confirmed state.

A UI action that was not re-verified by snapshot did not happen — there is no HTTP status code here. If the post-save snapshot doesn't show the new value, record that change as **failed**, do not retry it blindly, and do not assume success. If the campaign is SmartROI and the edit control is disabled or the value snaps back, record it as failed with that reason rather than treating a no-op as success.

## 8. Record every applied change

For each change that verification in step 7 actually confirmed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" record --kind bids --payload <payload.json>
```

`payload.json` must carry `before` captured from the pre-edit snapshot in step 7.2, so `mp_state.py rollback` can compute the inverse mechanically. Full sequence, no exceptions:

```
check-halt -> cooldown check -> reviewer verdict -> apply -> verify (re-snapshot) -> record
```

## 9. Always close the session

Call `browser_close` at the end of this skill's work, whether it finished normally, hit the step-3 hard stop, or hit any other error partway through. A left-open authenticated session is a standing liability — close it every time, even on failure.
