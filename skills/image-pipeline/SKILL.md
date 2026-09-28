---
name: image-pipeline
description: Generate product image variants for A/B creative testing on Amazon and Flipkart, gate every variant through deterministic compliance checks and a visual policy review, and promote only approved assets. Use this whenever a SKU needs new hero/secondary image variants, when a generated image needs compliance/approval before touching a catalog, or when attributing CTR shifts to a live image variant. Never makes the approve/reject call itself — that always goes to image-approver.
allowed-tools: Bash, Read, Write, Agent, mcp__ask-gemini__generate_image
---

# Image Pipeline

Generate → validate → approve → promote. You never decide compliance yourself; you only generate,
delegate, and move files after a verdict says you may.

## 1. Inputs

- Target SKUs and thresholds come from `config/skus.json` at the project root (`ab_min_impressions_per_variant`,
  `ab_min_days` live under `defaults` there — never hardcode them here).
- Source assets live in `assets/raw/`.

**`assets/raw/` is IMMUTABLE.** Read only. Never write, move, rename, or delete anything in it, even
to "clean up" a failed run. A PreToolUse hook blocks writes to this path — if a command of yours gets
denied here, that is the guardrail working, not a bug to route around. Do not retry with a different
tool or a workaround path.

## 2. Generation

Use the `ask-gemini` MCP server, tool `mcp__ask-gemini__generate_image`, to produce variants into
`assets/generated/`. Naming: `<sku>_variant_<letter>.<ext>` (e.g. `SKU123_variant_a.jpg`).

**Quota is a hard stop, not something to retry around.** Gemini image models are not available on
the free tier — an unbilled key returns HTTP 429 with `limit: 0` and a `FreeTier` quota id. That is
a billing state, not congestion, so retrying will never clear it. On any 429 from
`generate_image`, distinguish the two cases by the reported limit:

- `limit: 0` → billing is not enabled. Record a blocker naming that, skip image work for the whole
  run, and continue with ads and pricing. Do not retry.
- a non-zero limit with a `retryDelay` → genuine rate limiting. Wait the stated delay, retry once,
  then record a blocker and move on.

Never loop on quota errors; each attempt costs and none will succeed.

Prompt patterns aimed at CTR, pick per slot:
- **Contrasting studio lighting** — product on seamless white, single dramatic key light plus fill,
  strong shadow falloff to make the product pop without changing its actual colour.
- **Feature callout composition** — product framed to foreground its most distinctive physical
  feature (texture, closure, silhouette) via camera angle and depth of field, not overlay graphics.
- **Contextual staging** — product shown in a realistic, plausible use setting (secondary slots only).

Every prompt must encode these constraints explicitly, regardless of pattern:
- no text of any kind burned into the image
- no promotional badges, stickers, or ribbons
- no watermarks
- no borders or decorative frames
- no props or accessories that are not included in the purchase
- white background for any Amazon hero/MAIN shot

## 3. Validation + approval — the decision layer

This skill does not make the approval decision. For every generated variant, delegate to
`@agent-marketplace-agent:image-approver` with the file path, SKU, channel, and variant label. That
agent runs `image_check.py` (deterministic pixel checks) and separately reads the image itself
(subjective policy: text, badges, watermarks, props, MAIN-slot white-background rule). Both gates
must pass for `approved`.

Do not second-guess a rejection, and do not re-run the same variant through the approver hoping for a
different answer — a rejection is final for that file. If you disagree with the reasons, that is
feedback for the next *generation* attempt (step 6), not grounds to re-submit the same asset.

## 4. Promotion

Only a variant with `"verdict":"approved"` in `analytics/decisions/image_approvals.jsonl` may be
copied to `assets/approved/`. Check the verdict file yourself before copying — do not trust a verbal
"looks fine."

```bash
cp "assets/generated/<sku>_variant_<letter>.jpg" "assets/approved/"
```

Copy, never move. The generated original stays in `assets/generated/` for audit; nothing is ever
promoted out of `assets/raw/` since that directory is never a source of writes to begin with.

## 5. Hosting gate

**There is no image upload endpoint.** The Listings Items API takes a `media_location` **URL**, and
**Amazon's own crawler fetches the file from you**. You never hand Amazon the bytes. So the approved
asset must sit at a URL Amazon can reach over the public internet at ingestion time.

That is the whole requirement. It does **not** need a CDN, and it does not need to be permanent:

- Any publicly readable `https://` or `s3://` URL works — an S3 bucket with public read is the
  simplest, and an existing web server you already run is equally fine.
- A private S3 bucket also works, but only if you grant read to Amazon's media-download role
  (`arn:aws:iam::368641386589:role/Media-Download-Role`). A plain private link fails.
- It must need no auth: a signed URL that expires, a login-walled path, or an IP-restricted host
  fails **silently**, because Amazon fetches asynchronously with its own crawler. "It opens in my
  browser" proves nothing — your browser has your session.
- Once ingested, Amazon re-hosts the image on its own infrastructure, so the URL only has to be
  live at ingestion time.

**Do not delete old variants after a swap.** Rollback restores the previous `media_location`, and
that URL has to still resolve for the revert to work.

Flipkart needs none of this — its images cannot be set through any API at all.

Upload each approved asset, then record the resulting URL alongside the SKU and variant in the same
promotion record you write for Phase 4 attribution.

**The destination is deployment-specific — do not invent one.** The operator sets an env var naming
the bucket or base URL. If it is unset, treat hosting as unconfigured: stop before calling
`amazon-sp` for that SKU, record a blocker, and move to the next SKU. Never submit a local path.

## 6. Rejection handling

On a `rejected` verdict:
1. Record the SKU, variant, and `reasons` verbatim.
2. Retry generation with the rejection reasons fed back into the prompt, up to 2 additional attempts
   for that SKU/slot (3 attempts total).
3. If all attempts are rejected, stop generating for that SKU/slot, record it as a blocker, and
   continue with the rest of the run. Never loop indefinitely — each attempt burns image-generation
   quota, and an unbounded retry loop on one SKU starves every other SKU in the run.

## 7. A/B bookkeeping

Record, per SKU: which variant went live, on which channel, and the timestamp it was patched into the
catalog. `amazon-ads` and `flipkart-ads` need this to attribute CTR shifts to the correct variant.

A winner cannot be called until both `ab_min_impressions_per_variant` (per variant) and `ab_min_days`
(elapsed since the patch timestamp) from `config/skus.json` are satisfied. Below either threshold the
comparison is inconclusive — report it as such and leave both variants running rather than calling a
premature winner.
