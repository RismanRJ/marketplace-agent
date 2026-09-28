---
name: amazon-sp
description: Inspects Amazon Seller Central listings via SP-API — reads catalog attributes and issues, and patches listing images (main/secondary) with compliance gating and post-patch verification. Use for anything involving an Amazon listing item, its productType/attributes, image_locator patches, ACCEPTED/INVALID submission status, or confirming an Amazon catalog change actually took.
allowed-tools: Bash, Read
---

# Amazon SP-API listings

This skill is **transport-agnostic**: Amazon's official Selling Partner MCP connector
(`sellingpartner-ai.amazon.com/mcp`) when it is connected, `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp ...`
from Bash when it is not. Policy — the pre-patch gate, mutation sequence, and verification rule — is
identical either way. Only the mechanics differ, called out below wherever they apply.

## 1. Choose the transport, before any operation

Check the available tool list for the connector's confirmed tools:
`accounts_getMerchants`, `search_tools`, `get_tool_schema`, `call_read_only_tool`,
`call_write_tool`, `sellerAssistant_sellerAssistantCreate`, `sellerAssistant_sellerAssistantGet`,
`functional_feedback`.

- **Connector tools present → connector is primary.** This is a **meta-tool gateway**, not one tool
  per operation — there is no `get-listing`-shaped tool sitting in the list. Every real SP-API
  operation is DISCOVERED then dispatched: `search_tools` for the operation you need → `get_tool_schema`
  on the match to see its exact request shape → dispatch through `call_read_only_tool` (reads) or
  `call_write_tool` (writes). Never assume an operation exists without finding it first. If nothing
  plausible turns up, **stop and record a blocker** — do not guess a tool name or invent parameters
  `get_tool_schema` didn't declare. Call `accounts_getMerchants` first, before anything else, to
  resolve which merchant/marketplace you're acting on — don't assume the account context.

- **Connector tools absent → fall back to `mp_api.py`**, exactly as documented in the rest of this
  file:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp get-listing --sku DEMO-TSHIRT-BLK-M
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp patch-listing \
    --sku DEMO-TSHIRT-BLK-M --patch-file /tmp/patch.json --dry-run
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp verify-listing --sku DEMO-TSHIRT-BLK-M
  ```

  All three print JSON to stdout and exit non-zero on failure. `mp_api.py` already retries 429/5xx
  with backoff — do not add your own retry loop on top of it.

Mixing transports per-operation is fine — e.g. connector for reads, `mp_api.py` for a patch it
doesn't expose (see §3).

**Availability**: Amazon's launch post described the connector as US-stores-first with international
expansion to follow; the operator's own process doc documents connecting it with no such caveat.
Attempt the connection regardless of marketplace; if it isn't offered for yours, fall back to the
refresh-token path (`mp_api.py`) — see `docs/amazon-access.md` for the full setup of both paths.

Credentials (LWA refresh token/client id/client secret, seller id) come from environment variables
that `mp_api.py` reads itself. Never `cat`/`grep`/echo `.env` or print a credential value — if you
need to know whether one is set, check for the variable's presence only. On the connector path, auth
is the already-established OAuth session — you don't handle tokens at all.

## 2. Identifiers

- `sellerId` — from env, never hardcoded, never printed. On the connector path, resolved instead via
  `accounts_getMerchants`.
- `sku` — pick it from `config/skus.json`'s `skus[]` entries where `channel == "amazon"`.
- `marketplaceIds` — required on every call. `mp_api.py` resolves it from that same SKU entry's
  `marketplace_id`; never pass or invent one yourself. India (amazon.in) is `A21TJRUUN4KGV`, served
  by the **EU** host (`sellingpartnerapi-eu.amazon.com`), not NA.

## 3. Image attributes

Main image: `main_product_image_locator`. Secondary: `other_product_image_locator_1` through `_8`.
Value is an **ARRAY**, even for a single image:

```json
[{"media_location": "https://cdn.example.com/approved/tshirt-blk-m-main-v2.jpg",
  "marketplace_id": "A21TJRUUN4KGV"}]
```

A bare object instead of an array is rejected. Reuse the `marketplace_id` from the same config
lookup as above — don't retype it by hand.

**UNVERIFIED**: whether the connector exposes a raw catalog image-patch primitive at all, or only
higher-level "listing diagnosis and fixes" flows. Use `search_tools` to look for a listings/catalog
patch operation before assuming one exists. If none is exposed, fall back to
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp patch-listing` for image work
specifically, even when the connector is connected and used for everything else on this SKU.

## 4. `productType` is required

The PATCH body carries `productType` at the top level even when the only change is an image, on
either transport:

```json
{"productType": "SHIRT",
 "patches": [{"op": "replace", "path": "/attributes/main_product_image_locator", "value": [
   {"media_location": "https://cdn.example.com/approved/tshirt-blk-m-main-v2.jpg",
    "marketplace_id": "A21TJRUUN4KGV"}]}]}
```

Get it off the existing listing — a `get-listing` call (or the connector's discovered read
equivalent) carries `productType`/`summaries` data. Read it from there; never guess it or carry it
over from memory between SKUs. A missing/omitted `productType` is a common, silent cause of
`INVALID`. If you can't find it, stop and record a blocker for that SKU rather than patch without it.

## 5. Pre-patch gate — mandatory, no exceptions

An image may be patched into a listing only if BOTH hold:

1. An `"verdict": "approved"` line for that exact `asset`/`sku`/`channel`/`variant` exists in
   `analytics/decisions/image_approvals.jsonl`, written by the image-approver agent. Read the file
   yourself and check the most recent matching line — no verdict, a `rejected` verdict, or a
   verdict for a different variant all count as **not approved**.
2. The `media_location` you are about to write is a public `https://` or `s3://` URL Amazon's own
   crawler can fetch with no auth and no IP allowlisting — it fetches asynchronously, later, with its
   own network, not yours. A local path (`assets/approved/...`) can **never** work. If the approved
   file only exists locally, upload it to public hosting first (e.g. the S3 bucket already used for
   `analytics/`) and patch with the resulting URL — "it opens in my browser" proves nothing about
   whether Amazon's crawler can reach it; auth-walled or IP-restricted URLs fail silently.

## 6. Mutation sequence

Fixed order, every time, per SKU, on either transport:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" check-halt || exit 1

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" cooldown check \
  --entity DEMO-TSHIRT-BLK-M --hours 72 || exit 1   # 72 = defaults.cooldown_hours in config/skus.json
```

Capture the `before` value before any write, so rollback stays mechanical — via `get-listing` on the
`mp_api.py` path, or the discovered read tool through `call_read_only_tool` on the connector path.

**On the `mp_api.py` path**, `--dry-run` on `patch-listing` is **fully offline**: it prints the exact
request that would be sent, with secrets redacted, and sends nothing. It shows
`mode=VALIDATION_PREVIEW` in the previewed params, but it does not call Amazon, so it cannot return
`VALID`/`INVALID` — use it to check the payload you built, not to validate against the catalog. Drop
`--dry-run` only after confirming the printed request matches what you expect.

**On the connector path, there is no `--dry-run`.** A write dispatched through `call_write_tool` is a
live mutation — never call it "just to see what happens." `check-halt`, `cooldown check`, and the
approved-image verdict in §5 are what stand in for a dry run on this transport. The connector also
drafts writes for its own approval step and keeps its own audit trail — additional to, not a
substitute for, this plugin's audit log in §7.

Server-side validation happens on the real apply either way: the PATCH responds synchronously with
`ACCEPTED` or `INVALID`. `INVALID` is a hard stop for this SKU — parse `issues`, record a blocker, and
move to the next SKU. Nothing propagates on `INVALID`, so a rejected apply is a safe no-op.

## 7. Record every applied change

- **`mp_api.py` path**: it writes the audit record itself for every mutating call, keyed on the
  entity, with your `before` value, only when the vendor actually accepted the write. Do **NOT** run
  `mp_state.py record` yourself after it — a second record double-counts the change and can start a
  false cooldown.
- **Connector path**: a `call_write_tool` invocation does **NOT** write an audit record — it's just a
  vendor write. You must call it yourself, immediately after every applied change, with the `before`
  value captured **before** the write:

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" record --kind listing_patches --payload <file>
  ```

  Skipping this leaves cooldown and rollback blind — they read only the audit log, not Amazon. This
  is the same trap already documented for the Ads MCP path in `skills/amazon-ads/SKILL.md` §8.

The kind either transport ultimately records under is `listing_patches`.

## 8. Verification — ACCEPTED is not the answer

`patch-listing`'s synchronous response (`{sku, submissionId, status, issues[]}`, status `ACCEPTED`
or `INVALID`) only means the patch passed sync validation, not that the catalog actually changed.
There is no submission-status endpoint to poll. Confirm by re-fetching — via `verify-listing` on the
`mp_api.py` path, or the discovered read tool with `includedData=issues,summaries` through
`call_read_only_tool` on the connector path:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp verify-listing --sku DEMO-TSHIRT-BLK-M
```

Check `summaries[].status` and `issues[]` on that response, not the PATCH response. How long
propagation takes is undocumented — if the first re-check still shows the old value, wait briefly and
check again rather than assuming either failure or success; confirm a sane wait interval with the
operator instead of hardcoding one. Record and report whatever the re-check shows — an `ACCEPTED`
never confirmed by a re-GET is an unverified patch, not a done one.

## 9. Error handling

- `429`: already retried by `mp_api.py` with backoff honouring `Retry-After` (rate limits are 5 rps,
  burst 10 for GET / 5 for PATCH, reported via the `x-amzn-RateLimit-Limit` header) — nothing for
  you to do here. On the connector path, no rate limits are documented — back off on anything that
  looks like throttling and never assume a large batch is safe to fire quickly.
- `INVALID` (from dry-run or apply) or a bad verification re-check: parse `issues[]`, stop working
  on **this SKU only**, write a blocker record (reason = the issue text), and move on to the next
  SKU. Never abort the whole run over one bad listing.

## Unverified — say so, don't paper over it

- Whether the connector exposes a raw catalog image-patch primitive at all, or only wraps one inside
  higher-level "listing diagnosis and fixes" flows (see §3).
- The exact `issues[].severity` enum spelling (ERROR/WARNING/INFO is an assumption, not confirmed).
- How long to wait before a post-patch verification reliably reflects the change.

If any of these matter for a decision you're making, tell the operator you're going on an assumption
rather than stating it as fact.
