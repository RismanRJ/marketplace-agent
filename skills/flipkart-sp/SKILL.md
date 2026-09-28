---
name: flipkart-sp
description: Reads Flipkart Seller API listing details, revises selling price/MRP, and updates location-level inventory via the v3 Listings API — with halt/cooldown/dry-run gating before any write. Use for anything involving a Flipkart SKU/FSN, listing_status, update/price, update/inventory, or confirming a Flipkart listing change actually took. Does NOT cover Flipkart listing images (portal-only, see section 2) or Flipkart Ads (no public API).
allowed-tools: Bash, Read
---

# Flipkart Seller API — listings, price, inventory

There are no `http_request` or `sp_*` MCP tools. Every call goes through Bash:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart <subcommand> ...
```

Credentials (`appId`/`appSecret` for HTTP Basic, used to mint an OAuth2 `client_credentials` access
token) come from environment variables that `mp_api.py` reads and refreshes itself, caching the token
to disk with its `expires_in` expiry. Never `cat`/`grep`/echo `.env`, never print a token, and never
hardcode a token or a TTL — a static token breaks the moment it expires. If you need to know whether a
credential is set, check for the variable's presence only.

## 1. Subcommands

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart get-listings --skus DEMO-TSHIRT-BLK-M,DEMO-TSHIRT-BLK-L

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-price \
  --file /tmp/price_patch.json --dry-run

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-inventory \
  --file /tmp/inventory_patch.json --dry-run
```

All three print JSON to stdout and exit non-zero on failure. `mp_api.py` already retries 500/503/599
with backoff — do not add your own retry loop on top of it.

## 2. IMAGES ARE OUT OF SCOPE — read this before doing anything else

Flipkart listing images **cannot be set or changed through any documented Seller API.** No image
field exists on any writable v3 payload (create, update, update/price, update/inventory), the Seller
FTP feed has no image column, and the only image field anywhere is a **read-only**
`product_image_url` returned by `product/search`. Image changes are portal-only — Catalog Manager
manual edit or the Excel bulk-catalog-upload flow — i.e. browser-automation work, not this skill's
job. **Never report that this skill patched, replaced, or updated a Flipkart image**; if a task needs
one, say so and hand it off.

The asset guidelines (minimum 500x500px, recommended 1000x1000px, solid background, no text/promo
overlays) still apply, but only as the bar the image pipeline (`image_check.py`) must clear *before*
a human or a separate browser-automation flow uploads the file through the portal.

## 3. Identifiers

- **SKU** — seller-chosen, immutable after creation. The key for nearly every read and write; pull it
  from `config/skus.json`'s `skus[]` entries where `channel == "flipkart"`.
- **FSN** — Flipkart's catalog product id (13-16 alphanumeric). Trap: in every v3 payload the field
  literally named `product_id` **is the FSN**, not a listing id. Read it off a `get-listings` response
  and pass it back verbatim on price/inventory writes — never guess or reuse it across SKUs.
- **listing_id** — `LST<FSN><suffix>`, Flipkart-generated per seller-listing. Returned by reads only;
  it is **never an input key** on any v3 create/update call. One FSN maps to many seller listings,
  each with one SKU and one listing_id.

## 4. Reading listings

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart get-listings --skus DEMO-TSHIRT-BLK-M
```

Calls `POST /sellers/listings/v3/details` with `{"sku_ids":["DEMO-TSHIRT-BLK-M"]}` — max 10 SKUs per
batch, chunk larger sets yourself. Response shape:

```json
{"available": {"DEMO-TSHIRT-BLK-M": {"listing_id": "LST...", "product_id": "<FSN>",
    "product_title": "...", "price": {"mrp": 1999, "flipkart_selling_price": 1499, "currency": "INR"},
    "listing_status": "ACTIVE", "archived_status": "NONE", "locations": [], "packages": []}},
 "unavailable": ["<sku not found>"],
 "invalid": ["<malformed sku>"]}
```

There is no `invalid_skus` and no `inactive_listings` key. An inactive listing still appears **inside
`available`** with `listing_status: "INACTIVE"` — check that field, don't assume presence in
`available` means active. `unavailable` means the SKU was not found at all; `invalid` means the SKU
string itself was malformed. A `401` is a distinct "Client unauthorized" error, separate from `400`.

## 5. Price updates

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-price --file /tmp/price_patch.json --dry-run
```

Payload keyed by SKU, max 10 per call:

```json
{"DEMO-TSHIRT-BLK-M": {"product_id": "<FSN>", "price": {"mrp": 1999, "selling_price": 1499, "currency": "INR"}}}
```

UNVERIFIED: the newer authoritative schema names the field `selling_price` (snake_case); the older
doc portal shows `sellingPrice` (camelCase). Confirm which one the API actually accepts with one real
call before relying on either — do not silently pick one and assert it's correct.

Response is a per-SKU map: `{"<sku>": {"status": "SUCCESS|FAILURE|WARNING", "errors": [...], "attribute_errors": [...]}}`.
**You must check every SKU's `status` individually** — an HTTP 200 on the whole batch does not mean
every SKU in it succeeded; a batch can come back 200 with some entries `FAILURE`.

## 6. Inventory updates

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-inventory --file /tmp/inventory_patch.json --dry-run
```

Payload keyed by SKU, max 10 per call:

```json
{"DEMO-TSHIRT-BLK-M": {"product_id": "<FSN>", "locations": [{"id": "<loc>", "status": "ENABLED", "inventory": 50}]}}
```

Same per-SKU `status` response shape and the same rule: check every SKU, don't trust the batch HTTP
code alone.

## 7. Mutation sequence — fixed order, every time, per SKU/batch

check-halt -> cooldown -> details read for `before` -> dry-run -> apply -> record.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" check-halt || exit 1

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_state.py" cooldown check \
  --entity DEMO-TSHIRT-BLK-M --hours 72 || exit 1   # 72 = defaults.cooldown_hours in config/skus.json

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart get-listings --skus DEMO-TSHIRT-BLK-M > /tmp/before.json

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-price --file /tmp/price_patch.json --dry-run
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mp_api.py" flipkart update-price --file /tmp/price_patch.json
```

Carry `before` (from the details read) and a `reason` on each entity in the `--file` batch. `mp_api.py` writes the audit record itself for every mutating call, keyed on the entity, with your `before` value and only when the vendor actually accepted the write. Do NOT run `mp_state.py record` after it — a second record double-counts the change and can start a false cooldown.
It writes kind `price` for price updates and `inventory` for stock updates, and it records a SKU
**only** when that SKU's own response `status` is `SUCCESS` — a `FAILURE`/`WARNING` SKU never enters
the audit log, so it neither starts a cooldown nor appears in a rollback.

## 8. Errors and limits

Documented codes: `200`, `202`, `400`, `403`, `404`, `422`, `500`, `503`, `599` (connection timeout),
plus `401` for auth failure. Flipkart publishes **no rate limits, no 429, and no `Retry-After`** —
repeated `503` is the only backoff signal there is. Retry `500`/`503`/`599` with exponential backoff
and jitter (already handled by `mp_api.py`); treat `400`/`403`/`404`/`422` as non-retryable — fix the
request instead of retrying it. Client timeout must be >= 10s. The batch cap of 10 SKUs is a hard API
limit on every v3 endpoint here — chunk larger sets, never send more than 10 in one call.

## 9. Sandbox is effectively unavailable

The legacy sandbox host returns 503 on every path when tested live, and no other sandbox host is
published for Listings. Treat sandbox as unavailable — `--dry-run` against production (validated
locally by `mp_api.py`, not sent) is the primary safety mechanism here, not a sandbox call.
