# Marketplace Ads Agent — Operational Directives

> **Template note:** this file was copied to the project root by the `marketplace-init` skill.
> Before your first live run, review the autonomy boundary below and the thresholds in
> `config/skus.json` — they are the actual guardrails this agent runs under.

Unified, largely autonomous management of Amazon and Flipkart listings and advertising:
product image A/B testing, catalog mutations, ad performance monitoring, and unified telemetry.

All capability ships as the **`marketplace-agent` plugin** in `./marketplace-agent/`.
Enable it with `claude --plugin-dir ./marketplace-agent`.

## Ground truth about this environment

Read this before trusting any workflow description.

- **Amazon Ads**: connect Amazon's official Ads MCP server (remote HTTP, open beta) — see the
  plugin README for the `claude mcp add` command and region notes. Prefer its tools when they are
  present in the session; fall back to `${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py amazon-ads ...` when
  they are not.
  **Its tool names are not published** — discover them at runtime by tool group
  (`account_management`, `billing`, `campaign_management`, `reporting`). Never hardcode an Ads MCP
  tool name, and never guess one: if no tool clearly does the operation, record a blocker and stop.
- **Amazon SP-API (listings/catalog)** has no MCP path available. Amazon's Selling Partner
  connector exists but is **US stores only** in beta, so it does not cover amazon.in (confirm
  coverage for your own marketplace before relying on it). SP-API is reached over HTTPS through
  `mp_api.py amazon-sp ...`.
- Do not assume any `sp_*` / `ads_*` style tool name exists until you have discovered it at runtime.
- **An MCP tool call does not write an audit record.** `mp_api.py` does that itself; MCP does not.
  On any MCP path you must call `mp_state.py record` explicitly, or cooldown and rollback go blind.
- **Flipkart has no public advertising API.** Ads telemetry and bid/budget changes happen through
  the Playwright MCP browser against the seller portal. This data is scraped, not authoritative.
  Flipkart reports **ROI as a multiple** (revenue ÷ spend), not ACoS.
- Flipkart listings/price/inventory *do* have a real API, also via `mp_api.py`.
- **Flipkart listing images cannot be changed through any API.** No image field exists on any
  writable Seller API v3 payload, and the FTP feed has no image column. Image changes are
  portal-only (Catalog Manager, or the Excel bulk-upload flow). Treat Flipkart image A/B as
  browser-automation work or as out of scope — never claim an API patched a Flipkart image.
- Image generation uses the `ask-gemini` MCP server — connect it before running `image-pipeline`.

## Skill routing

| Need | Skill |
|---|---|
| First-time setup — scaffold this project | `marketplace-init` |
| Run a full autonomous cycle | `marketplace-run` |
| Generate + approve image variants | `image-pipeline` |
| Amazon listings / catalog / image patches | `amazon-sp` |
| Amazon Sponsored Products telemetry + bids | `amazon-ads` |
| Flipkart listings / price / inventory | `flipkart-sp` |
| Flipkart ads (browser automation) | `flipkart-ads` |
| Undo a bad change | `marketplace-rollback` |

Decision layer (never bypass): `@agent-marketplace-agent:image-approver` gates every image,
`@agent-marketplace-agent:bid-reviewer` gates every bid and budget change.

## Single source of truth

**All thresholds live in `config/skus.json`.** Target ACoS, bid caps, floors and ceilings,
cooldowns, minimum sample sizes, per-run change limits, image thresholds. Never hardcode a
threshold in a skill, an agent, or a script, and never restate a number from that file as if it
were policy — read it. `config/skus.json` is also the list of SKUs in scope; a SKU not in that
file is out of scope.

## Autonomy boundary

This agent is designed to run unsupervised. That is only safe because the boundary is explicit.

**Autonomous — no human needed:** telemetry pulls, image generation, compliance checks, image
approval/rejection, bid and budget changes *within* the configured caps, price and inventory
updates, reporting, dashboard generation, and rollback of its own changes.

**Never autonomous — hard stop, every time:**
- CAPTCHA, OTP, 2FA, or any login or credential entry
- any change exceeding a cap in `config/skus.json`
- creating or terminating campaigns, or changing anything not listed in `config/skus.json`
- any action at all while a `HALT` file exists at the project root

On a hard stop: mark **that channel** degraded, write a blocker record, notify, and **continue the
other channel**. Finish the run and report. Never block an entire run waiting on a human — a
half-finished run that silently stalls is worse than a partial run that reports honestly.

## Invariants

1. **Raw assets are immutable.** Never write, move, or delete anything under `assets/raw/`.
   A PreToolUse hook enforces this; do not try to work around it.
2. **Never read or print credentials.** Not `.env`, not secret files, not tokens in logs or
   screenshots. Credentials come from environment variables only (see the plugin README).
3. **Every mutation follows the same sequence, without exception:**
   `check-halt → cooldown check → dry-run → decision-layer verdict → apply → record`
   A mutation with no audit record did not happen correctly, even if it succeeded.

   The audit `kind` vocabulary is closed. Use exactly these strings, nothing else:
   `bids`, `budgets`, `listing_patches`, `price`, `inventory`, `image_approval`,
   `bid_review`, `blocker`. A kind outside this list writes to a file nothing reads.

   **"Record a blocker" means literally this** — it is not just phrasing for the run summary:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" record --kind blocker --payload <file>
   ```
   with `channel` set, `reason` explaining what stopped, and `entity_id` naming the SKU, campaign
   or channel affected. This is the only way a degraded channel or a hard stop reaches the
   dashboard and the run report. An unrecorded blocker is invisible to whoever reads the run
   afterwards, which defeats the point of running unattended.
4. **Every mutation records its `before` value** so rollback is mechanical rather than guesswork.
5. **One run at a time.** Acquire the lock via `mp_state.py lock acquire`; concurrent runs
   double-apply bid changes.
6. **Label data provenance everywhere** — in every export and in `dashboard/unified_view.html`:
   - Amazon → `Live API (SP-API / Ads API)`
   - Flipkart ads → `Autonomous Browser Session`
   Never present scraped numbers and API numbers as equivalent.

## Metric discipline

- `ACoS % = spend / sales × 100`. `RoAS = sales / spend`. They are reciprocals: 25% ACoS = 4.0× RoAS.
- Flipkart's dashboard "ROI" is **revenue ÷ spend**, i.e. RoAS — not ROI in the accounting sense.
  Convert explicitly and label it; do not print it next to an ACoS as though they were the same thing.
- **Do not compare Amazon and Flipkart performance directly.** Attribution windows and models
  differ. Compare each channel against its own target and its own history.
- Never act on a sample below `min_impressions`. An A/B winner requires `ab_min_impressions_per_variant`
  and `ab_min_days` before it is called; a CTR delta on thin traffic is noise, not a result.

## Layout

```
CLAUDE.md                          # this file
HALT                               # if present, all automation refuses to run (kill switch)
config/skus.json                   # SKUs in scope + ALL thresholds
assets/{raw,generated,approved}/   # raw is immutable; approved requires an approver verdict
analytics/
  audit/*.jsonl                    # every mutation, with before/after — the rollback source
  proposals/                       # bid change sets awaiting review
  decisions/                       # image_approvals.jsonl, bid_reviews.jsonl
  amazon_live_pulls/               # API pulls
  flipkart_snapshots/              # scraped metrics + screenshot proofs
dashboard/unified_view.html        # generated; never hand-edit
marketplace-agent/                 # the plugin (skills, agents, hooks, scripts)
```

## Reporting

Report honestly. If a channel was degraded, say which and why. If a change was rejected by the
decision layer, say so and give the reason. If something was skipped because of a cooldown or cap,
say that rather than omitting it. A run summary that hides a failure is a bug.
