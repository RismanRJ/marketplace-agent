---
name: amazon-sp
description: Inspects Amazon Seller Central listings via SP-API — reads catalog attributes and issues, and patches listing images (main/secondary) with compliance gating and post-patch verification. Use for anything involving an Amazon listing item, its productType/attributes, image_locator patches, ACCEPTED/INVALID submission status, or confirming an Amazon catalog change actually took.
allowed-tools: Bash, Read
---

# Amazon SP-API listings

There are no `sp_*` MCP tools — those names in the first draft were never real.

Amazon does now ship an official **Selling Partner MCP connector**
(`sellingpartner-ai.amazon.com/mcp`), but it is **US stores only** in beta, so it does not cover
amazon.in and is not a transport option here. It is also a meta-tool gateway (`search_tools`,
`get_tool_schema`, `call_read_only_tool`, `call_write_tool`) rather than one tool per operation, and
it is unconfirmed whether it exposes a raw catalog image-patch primitive at all. If it opens up in
your marketplace, re-check that before assuming image patching can move to it.

Until then, every call goes through Bash:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp <subcommand> ...
```

Credentials (LWA refresh token/client id/client secret, seller id) come from environment variables
that `mp_api.py` reads itself. Never `cat`/`grep`/echo `.env` or print a credential value — if you
need to know whether one is set, check for the variable's presence only.

## 1. Subcommands

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp get-listing --sku DEMO-TSHIRT-BLK-M

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp patch-listing \
  --sku DEMO-TSHIRT-BLK-M --patch-file /tmp/patch.json --dry-run

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp verify-listing --sku DEMO-TSHIRT-BLK-M
```

All three print JSON to stdout and exit non-zero on failure. `mp_api.py` already retries 429/5xx
with backoff — do not add your own retry loop on top of it.

## 2. Identifiers

- `sellerId` — from env, never hardcoded, never printed.
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

## 4. `productType` is required

The PATCH body carries `productType` at the top level even when the only change is an image:

```json
{"productType": "SHIRT",
 "patches": [{"op": "replace", "path": "/attributes/main_product_image_locator", "value": [
   {"media_location": "https://cdn.example.com/approved/tshirt-blk-m-main-v2.jpg",
    "marketplace_id": "A21TJRUUN4KGV"}]}]}
```

Get it off the existing listing — `get-listing`'s response carries `productType`/`summaries` data.
Read it from there; never guess it or carry it over from memory between SKUs. A missing/omitted
`productType` is a common, silent cause of `INVALID`. If you can't find it, stop and record a
blocker for that SKU rather than patch without it.

## 5. Pre-patch gate — mandatory, no exceptions

An image may be patched into a listing only if BOTH hold:

1. An `"verdict": "approved"` line for that exact `asset`/`sku`/`channel`/`variant` exists in
   `analytics/decisions/image_approvals.jsonl`, written by the image-approver agent. Read the file
   yourself and check the most recent matching line — no verdict, a `rejected` verdict, or a
   verdict for a different variant all count as **not approved**.
2. The `media_location` you are about to write is a public `https://` or `s3://` URL Amazon's own
   crawler can fetch with no auth and no IP allowlisting — Amazon fetches it asynchronously, later,
   with its own network, not yours. A local path (`assets/approved/...`) can **never** work and
   must not be written into a patch. If the approved file only exists locally, upload it to public
   hosting first (e.g. the S3 bucket already used for `analytics/`) and patch with the resulting
   URL — "it opens in my browser" proves nothing about whether Amazon's crawler can reach it;
   auth-walled or IP-restricted URLs fail silently.

## 6. Mutation sequence

Fixed order, every time, per SKU. `--dry-run` on `patch-listing` is **fully offline**: it prints the
exact request that would be sent, with secrets redacted, and sends nothing. It shows
`mode=VALIDATION_PREVIEW` in the previewed params, but it does not call Amazon, so it cannot return
`VALID`/`INVALID` — use it to check the payload you built, not to validate against the catalog.

Server-side validation happens on the real apply: the PATCH responds synchronously with `ACCEPTED`
or `INVALID`. `INVALID` is a hard stop for this SKU — parse `issues`, record a blocker, and move to
the next SKU. Nothing propagates on `INVALID`, so a rejected apply is a safe no-op.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" check-halt || exit 1

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" cooldown check \
  --entity DEMO-TSHIRT-BLK-M --hours 72 || exit 1   # 72 = defaults.cooldown_hours in config/skus.json

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp get-listing --sku DEMO-TSHIRT-BLK-M \
  > /tmp/before.json   # captured so rollback is mechanical

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp patch-listing \
  --sku DEMO-TSHIRT-BLK-M --patch-file /tmp/patch.json --dry-run   # offline: prints, sends nothing

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp patch-listing \
  --sku DEMO-TSHIRT-BLK-M --patch-file /tmp/patch.json   # real apply, only after VALID above
```

Put the `before` value (and a `reason`) into the patch file; `mp_api.py` writes the audit record itself for every mutating call, keyed on the entity, with your `before` value and only when the vendor actually accepted the write. Do NOT run `mp_state.py record` after it — a second record double-counts the change and can start a false cooldown.
The kind it writes is `listing_patches`.

## 7. Verification — ACCEPTED is not the answer

`patch-listing`'s synchronous response (`{sku, submissionId, status, issues[]}`, status `ACCEPTED`
or `INVALID`) only means the patch passed sync validation, not that the catalog actually changed.
There is no submission-status endpoint to poll. Confirm by re-fetching:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" amazon-sp verify-listing --sku DEMO-TSHIRT-BLK-M
```

This re-GETs with `includedData=issues,summaries`. Check `summaries[].status` and `issues[]` on
that response, not the PATCH response. How long propagation takes is undocumented — if the first
`verify-listing` still shows the old value, wait briefly and check again rather than assuming
either failure or success; confirm a sane wait interval with the operator instead of hardcoding
one. Record and report whatever `verify-listing` shows — an `ACCEPTED` never confirmed by a re-GET
is an unverified patch, not a done one.

## 8. Error handling

- `429`: already retried by `mp_api.py` with backoff honouring `Retry-After` (rate limits are 5 rps,
  burst 10 for GET / 5 for PATCH, reported via the `x-amzn-RateLimit-Limit` header) — nothing for
  you to do here.
- `INVALID` (from dry-run or apply) or a bad `verify-listing` re-GET: parse `issues[]`, stop working
  on **this SKU only**, write a blocker record (reason = the issue text), and move on to the next
  SKU. Never abort the whole run over one bad listing.

## Unverified — say so, don't paper over it

- The exact `issues[].severity` enum spelling (ERROR/WARNING/INFO is an assumption, not confirmed).
- How long to wait before a post-patch `verify-listing` reliably reflects the change.

If either matters for a decision you're making, tell the operator you're going on an assumption
rather than stating it as fact.
