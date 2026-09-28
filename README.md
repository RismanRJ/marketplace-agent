# marketplace-agent

A Claude Code plugin for largely autonomous Amazon + Flipkart listing and advertising management:
image A/B testing, catalog mutations, reviewed bid optimisation, and unified telemetry.

## Install

```bash
claude plugin marketplace add RismanRJ/marketplace-agent
claude plugin install marketplace-agent@rismanrj-plugins
```

The repo is both the plugin and its marketplace, so that one repo is all you need. It is private —
you need read access and working git credentials.

Or run it straight from a checkout, without installing:

```bash
claude --plugin-dir ./marketplace-agent
claude plugin validate ./marketplace-agent --strict
```

**First-time setup:** in the directory you want the agent to work in, run
`/marketplace-agent:marketplace-init`. It scaffolds the directory tree, `config/skus.json` and
`CLAUDE.md`, runs every script's selfcheck, and reports which credentials are unset. It never
overwrites an existing config, so it is safe to re-run.

Operational directives live in `CLAUDE.md` at your **project** root — `marketplace-init` puts it
there from `templates/CLAUDE.md` (a `CLAUDE.md` inside a plugin is not loaded). Read it before
running anything.

## Dependencies

```bash
pip install requests Pillow
```

`requests` is used by `mp_api.py`, `Pillow` by `image_check.py`. `mp_state.py` and `dashboard.py` are
stdlib-only.

## Amazon Ads MCP server (preferred transport for ads)

Amazon's official Ads MCP server is a **remote HTTP server** in open beta. Connect it once and
`amazon-ads` will prefer it over the HTTP script:

```bash
claude mcp add --transport http amazon-ads https://advertising-ai-eu.amazon.com/mcp
```

Regional endpoints: `advertising-ai.amazon.com` (NA), `advertising-ai-eu.amazon.com` (EU),
`advertising-ai-fe.amazon.com` (FE). Auth is an OAuth browser flow on first connect.

> **Pick the right region.** For **amazon.in, use the EU host.** Amazon's classic Ads API has
> historically served India from EU, while FE is Japan/Australia/Singapore. India is not named
> explicitly in Amazon's MCP docs, so confirm by connecting and listing your profiles — if no
> amazon.in profile appears, you are on the wrong host.

You still need your own approved Ads API developer application (Partner or Direct Advertiser). The
MCP server sits on top of that; it does not replace the approval process.

**Its tool names are not published.** The skill discovers them at runtime by tool group
(`account_management`, `billing`, `campaign_management`, `reporting`). Nothing here hardcodes one.
The server is in beta, its rate limits are undocumented, and it can delete campaigns — the plugin
only ever adjusts bids and budgets within caps, and every change still passes `bid-reviewer`.

**Selling Partner (listings/catalog):** Amazon's Selling Partner MCP connector
(`sellingpartner-ai.amazon.com/mcp`) is **US stores only** in beta, so it does not cover amazon.in.
`amazon-sp` therefore stays on the HTTP path below.

## Credentials

### Where credentials live

Nothing in this plugin stores a long-lived credential. There are exactly two places secrets exist
at runtime:

| What | Where | Lifetime |
|---|---|---|
| Client ids, client secrets, refresh tokens | **Process environment only** — read via `os.environ`, never written to disk by this plugin | Set by whatever launches the agent |
| Minted **access** tokens | `.mp-cache/<name>.json`, file `0600`, dir `0700`, gitignored | Minutes to hours; refreshed at 80% of TTL |

`mp_api.py` deliberately knows nothing about *where* your secrets come from — it only reads env
vars. That keeps the secret source a deployment decision. Pick one:

- **Local/dev** — a `.env` file you `source` in your shell before launching. The agent never reads
  the file; your shell does. macOS Keychain via `security find-generic-password` works the same way.
- **Unattended/scheduled** — do **not** put secrets in a crontab or launchd plist in plaintext. Use
  a wrapper that fetches them at start and execs the agent. On AWS,
  that's Secrets Manager or SSM Parameter Store:
  ```bash
  #!/bin/bash
  set -euo pipefail
  eval "$(aws secretsmanager get-secret-value --secret-id marketplace-agent/creds \
          --query SecretString --output text \
          | python3 -c 'import json,sys,shlex;[print(f"export {k}={shlex.quote(v)}") for k,v in json.load(sys.stdin).items()]')"
  exec claude --plugin-dir ./marketplace-agent -p "/marketplace-agent:marketplace-run"
  ```
  Secrets stay in the process environment and never touch disk.

**Never** put credentials in `config/skus.json` and never commit them.

### Why the agent cannot read its own credentials

This agent has Bash *and* ingests untrusted text — scraped seller-portal content, marketplace
keyword strings, product titles. That is a prompt-injection surface pointed at a process holding
live API credentials. Storing secrets in the environment is correct, but the environment is
readable from inside the process, so the PreToolUse hook blocks the exfiltration paths:

- `env`, `printenv`, `export -p`, `declare -x` — environment dumps
- expanding any `$AMAZON_LWA_*`, `$AMAZON_ADS_*`, `$FLIPKART_APP_*`, or any var whose name contains
  `SECRET` / `REFRESH_TOKEN` / `ACCESS_TOKEN` / `API_KEY` / `PASSWORD`
- reading `.env*`, `*credential*`, `*secret*` files, and the `.mp-cache` token cache

`env VAR=value command` (the prefix form) still works. To check whether a variable is set, test
presence without printing the value: `[ -n "${AMAZON_SP_SELLER_ID:-}" ] && echo set`, or just run
`mp_api.py check-credentials`, which reports variable *names* only.

This is defence in depth, not a sandbox — a determined exfiltration could still encode a value past
a regex. The real boundary is that these credentials should be scoped to the marketplace APIs and
nothing else, and rotatable if you ever suspect a leak.

The `AMAZON_ADS_*` variables below are needed only for the `mp_api.py` fallback path. Once the Ads
MCP server is connected, it owns Ads authentication and those four become optional.

| Variable | Used by | Purpose |
|---|---|---|
| `AMAZON_LWA_CLIENT_ID` | SP-API | LWA app client id |
| `AMAZON_LWA_CLIENT_SECRET` | SP-API | LWA app secret |
| `AMAZON_LWA_REFRESH_TOKEN` | SP-API | Seller refresh token (access tokens last 1h and are auto-refreshed) |
| `AMAZON_SP_SELLER_ID` | SP-API | Selling partner / merchant id |
| `AMAZON_ADS_CLIENT_ID` | Ads API | Sent as `Amazon-Advertising-API-ClientId` |
| `AMAZON_ADS_CLIENT_SECRET` | Ads API | LWA secret for the Ads app |
| `AMAZON_ADS_REFRESH_TOKEN` | Ads API | Ads refresh token |
| `AMAZON_ADS_PROFILE_ID` | Ads API | Sent as `Amazon-Advertising-API-Scope`. Per marketplace/region |
| `FLIPKART_APP_ID` | Flipkart | OAuth2 app id (HTTP Basic) |
| `FLIPKART_APP_SECRET` | Flipkart | OAuth2 app secret (HTTP Basic) |

Tokens are cached under `.mp-cache/` (mode 0600) with their real `expires_in` and refreshed at 80%
of TTL, so unattended runs survive expiry. The cache is gitignored and never printed.

### Not yet wired: image hosting

There is no image upload endpoint. The Listings API takes a **URL**, and Amazon's crawler fetches
the file from you — you never hand Amazon the bytes.

You do **not** need a CDN, and the URL does not need to be permanent (Amazon re-hosts the image
after ingestion). You need one thing: a publicly readable `https://` or `s3://` URL, reachable with
**no auth**, at ingestion time. An S3 bucket with public read is enough. A private bucket works only
if you grant read to Amazon's media-download role
(`arn:aws:iam::368641386589:role/Media-Download-Role`).

A signed/expiring URL, a login wall or an IP restriction fails **silently**, because the fetch is
asynchronous and server-side — "it opens in my browser" proves nothing, your browser has your
session. And do not delete old variants: rollback restores the previous `media_location`, so that
URL has to still resolve.

The destination is **deployment-specific and not implemented here**. Point it at a bucket, wire the
upload into `image-pipeline`, and document the variable you use. Until then the Amazon image A/B
loop stops at the hosting gate and records a blocker, rather than pretending to succeed.
Flipkart needs none of this — its images cannot be set through any API.

## Components

| Path | What it is |
|---|---|
| `skills/marketplace-init` | First-time setup — scaffold a new project root. |
| `skills/marketplace-run` | The orchestration loop. Start here. |
| `skills/image-pipeline` | Generate → validate → approve → promote image variants |
| `skills/amazon-sp` / `amazon-ads` | Amazon listings / Sponsored Products |
| `skills/flipkart-sp` / `flipkart-ads` | Flipkart listings (API) / ads (browser) |
| `skills/marketplace-rollback` | Undo a previous run's changes |
| `agents/image-approver` | Decision layer: approve/reject one image variant |
| `agents/bid-reviewer` | Decision layer: adversarially review a bid/budget proposal |
| `scripts/mp_api.py` | The only network transport (Amazon SP, Amazon Ads, Flipkart) |
| `scripts/mp_state.py` | HALT switch, locks, cooldowns, audit log, rollback |
| `scripts/image_check.py` | Deterministic pixel compliance |
| `scripts/dashboard.py` | Unified telemetry HTML |
| `hooks/protect_raw.sh` | Blocks writes to `assets/raw/` and credential reads |

Every script has a `selfcheck` subcommand that runs offline:

```bash
for s in mp_api mp_state image_check dashboard; do python3 scripts/$s.py selfcheck; done
```

## Safety model

- **`HALT` file** at the project root is the kill switch — every script refuses to run while it exists.
- **Lock** (`mp_state.py lock acquire`) prevents overlapping runs double-applying bid changes.
- **Cooldown** (`cooldown_hours`) stops the same entity being adjusted every run until it hits zero.
- **Caps** (`max_bid_change_pct`, `bid_floor`, `bid_ceiling`, `max_changes_per_run`) bound blast radius.
- **Decision layer** — no image reaches a catalog without an `approved` verdict, no bid change is
  submitted without an `approve`/`amend` verdict. Both default to reject.
- **`--dry-run`** on every mutating command prints the exact redacted request and sends nothing.
- **Audit log** records `before` on every mutation, which is what makes rollback mechanical.

All thresholds live in `../config/skus.json`. Nothing is hardcoded in a skill, agent, or script.

### Shareable dashboard

Phase 5 of `marketplace-run` publishes `dashboard.py`'s output as a Claude Artifact so the team can
open a link instead of copying the local HTML file around. The artifact URL is stored in
`analytics/dashboard_url.txt` and reused on every later run, so republishing updates the same link
in place rather than minting a new one each time. It is **private by default** — a team member only
sees it if the link is shared with them. The dashboard contains seller financial data (ad spend,
revenue, ACoS, SKU performance), and publishing uploads that data to claude.ai; set
`"publish_dashboard": false` in `config/skus.json`'s `defaults` to keep the dashboard local-only and
skip publishing entirely.

## Known gaps — read before trusting this in production

1. **Nothing here has been run against a live Amazon or Flipkart account.** Every selfcheck is
   offline. Run at least one full `--dry-run` cycle with real credentials before letting it apply.
2. **`ponytail:` comments in `mp_api.py` mark 8 genuinely unverified API details** — Amazon Ads v3
   write body wrapper key and batch limit, report status enum spelling, NA/FE Ads hostnames,
   multi-status response shape, and Flipkart's `selling_price` vs `sellingPrice`. Confirm each
   against the live API or the OpenAPI spec; they are flagged, not guessed silently.
3. **Flipkart images cannot be changed by any API.** Portal-only. Not a bug to fix here.
4. **Flipkart ads automation has no verified DOM selectors.** The UI description is secondhand. Do
   one live authenticated Playwright pass before relying on it.
5. **Flipkart's sandbox is unreachable** (503 on every path tested), so `--dry-run` is the only
   pre-production safety net there.
6. **Image occupancy is a bounding-box heuristic**, not segmentation; it overestimates for products
   with disjoint parts and misfires on textured backgrounds.
