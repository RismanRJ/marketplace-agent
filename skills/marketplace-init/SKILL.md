---
name: marketplace-init
description: First-time setup for the marketplace-agent plugin — scaffold a fresh project root with the directory tree, config, CLAUDE.md and .gitignore this agent needs, verify dependencies and self-checks, and report which credentials are still missing. Use this for first-time setup, "set up the marketplace agent here", scaffolding a new marketplace-agent project, or onboarding a new install of this plugin.
allowed-tools: Bash, Read, Write
---

# Marketplace Init — First-time setup

Scaffolds the current working directory into a working marketplace-agent project root. Idempotent
— safe to re-run; it never overwrites `config/skus.json` or `CLAUDE.md` once they exist.

## 1. Create the directory tree

```bash
mkdir -p config assets/raw assets/generated assets/approved \
  analytics/audit analytics/proposals analytics/decisions \
  analytics/amazon_live_pulls analytics/flipkart_snapshots/screenshots \
  dashboard
```

## 2. Seed config — only if absent

```bash
[ -f config/skus.json ] || cp "${CLAUDE_PLUGIN_ROOT}/config/skus.example.json" config/skus.json
```

Never overwrite `config/skus.json` if it already exists — it holds the operator's real SKUs and
thresholds.

## 3. Seed the operational directives — only if absent

```bash
if [ -f CLAUDE.md ]; then
  echo "CLAUDE.md already exists — left untouched."
else
  cp "${CLAUDE_PLUGIN_ROOT}/templates/CLAUDE.md" CLAUDE.md
  echo "CLAUDE.md created from template — read it before running anything."
fi
```

## 4. Write `.gitignore` — only if absent

If `./.gitignore` does not exist, write one covering:

```
.env
.env.*
*credential*
*secret*
.mp-cache/
.mp-locks/
HALT
analytics/
assets/generated/
assets/approved/
dashboard/unified_view.html
__pycache__/
*.pyc
```

Do not touch an existing `.gitignore`.

## 5. Verify runtime dependencies

```bash
python3 -c "import requests, PIL" 2>&1 && echo "dependencies ok" || \
  echo "MISSING: run 'pip install requests Pillow'"
```

## 6. Run every script's selfcheck

```bash
for s in mp_api mp_state image_check dashboard; do
  echo "== $s =="
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/$s.py" selfcheck
done
```

Report pass/fail per script by name. A fresh install should show all four passing before anyone
points it at real money.

## 7. Check credentials

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" check-credentials --channel all
```

Report which channels are unconfigured, naming only the missing environment **variable names**
from the output (`missing_env`) — never a value, never attempt to read one.

## 8. Before your first live run

Tell the operator, plainly:

- Edit `config/skus.json` — set your real SKUs, campaign ids and thresholds.
- Set the credentials named above as environment variables (see the plugin README's Credentials
  section for what each one is and where it should live — do not restate it here).
- Connect the Amazon Ads MCP server (see the plugin README).
- Configure image hosting for Amazon (see the plugin README's "Not yet wired: image hosting").
- Run one full `--dry-run` cycle of `marketplace-run` before letting anything apply.

## Safe to re-run

This skill never overwrites an existing `config/skus.json` or `CLAUDE.md`, and never touches an
existing `.gitignore`. Re-running it only fills in what's missing and re-reports selfcheck/credential
status.
