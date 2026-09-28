#!/usr/bin/env python3
"""Network transport for Amazon SP-API, Amazon Ads API, and Flipkart Seller API.

No MCP tools exist for these APIs; every call from a skill goes through this file via Bash.

  amazon-sp  get-listing    --sku S
  amazon-sp  patch-listing  --sku S --patch-file F.json [--dry-run]
  amazon-sp  verify-listing --sku S
  amazon-ads report         --type sp-keyword --days 7 [--campaign-ids A,B] [--time-unit SUMMARY|DAILY]
  amazon-ads update-bids    --file F.json [--dry-run]
  amazon-ads update-budgets --file F.json [--dry-run]
  flipkart   get-listings   --skus A,B
  flipkart   update-price   --file F.json [--dry-run]
  flipkart   update-inventory --file F.json [--dry-run]
  check-credentials [--channel amazon|flipkart|all]
  selfcheck

JSON to stdout, human notes to stderr, non-zero exit on failure. --dry-run never touches the
network; it prints the exact request (headers redacted) and exits 0.

--file / --patch-file JSON shape (list of entities, or {"items": [...]}); each entity may carry an
optional "before" (recorded for rollback) and "reason" (recorded, never sent to the vendor):
  amazon-ads update-bids:      [{"keywordId": "...", "campaignId": "...", "bid": 0.72, "before": 0.85}]
  amazon-ads update-budgets:   [{"campaignId": "...", "budget": {...}, "before": 90.0}]
  flipkart update-price:       [{"sku": "...", "product_id": "<FSN>", "price": {...}, "before": {...}}]
  flipkart update-inventory:   [{"sku": "...", "product_id": "<FSN>", "locations": [...], "before": {...}}]
  amazon-sp patch-listing file is the raw PATCH body ({"productType", "patches": [...]}) plus an
  optional top-level "before"/"reason" (stripped before sending).
"""
import sys

try:
    import requests
except ImportError:
    print("Missing dependency: run 'pip install requests' and retry.", file=sys.stderr)
    sys.exit(1)

import argparse
import gzip
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mp_state  # noqa: E402


class CliError(Exception):
    pass


ENV_VARS = {
    "amazon_lwa_client_id": "AMAZON_LWA_CLIENT_ID",
    "amazon_lwa_client_secret": "AMAZON_LWA_CLIENT_SECRET",
    "amazon_lwa_refresh_token": "AMAZON_LWA_REFRESH_TOKEN",
    "amazon_sp_seller_id": "AMAZON_SP_SELLER_ID",
    "amazon_ads_client_id": "AMAZON_ADS_CLIENT_ID",
    "amazon_ads_client_secret": "AMAZON_ADS_CLIENT_SECRET",
    "amazon_ads_refresh_token": "AMAZON_ADS_REFRESH_TOKEN",
    "amazon_ads_profile_id": "AMAZON_ADS_PROFILE_ID",
    "flipkart_app_id": "FLIPKART_APP_ID",
    "flipkart_app_secret": "FLIPKART_APP_SECRET",
}

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
FLIPKART_TOKEN_URL = "https://api.flipkart.net/oauth-service/oauth/token"
FLIPKART_HOST = "https://api.flipkart.net"
FLIPKART_DETAILS_PATH = "/sellers/listings/v3/details"
FLIPKART_UPDATE_PRICE_PATH = "/sellers/listings/v3/update/price"
FLIPKART_UPDATE_INVENTORY_PATH = "/sellers/listings/v3/update/inventory"

USER_AGENT = "marketplace-agent/1.0 (Language=Python; Platform=CLI)"

SP_API_HOSTS = {
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "na": "https://sellingpartnerapi-na.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
# ponytail: only India (A21TJRUUN4KGV -> EU) is CONFIRMED by AMAZON.md. Add the real mapping for
# any other marketplace before onboarding it instead of trusting DEFAULT_SP_REGION.
MARKETPLACE_TO_SP_REGION = {"A21TJRUUN4KGV": "eu"}
DEFAULT_SP_REGION = "eu"

ADS_API_HOSTS = {
    "eu": "https://advertising-api-eu.amazon.com",
    # ponytail: AMAZON.md marks the NA/FE Ads API hostname spelling UNVERIFIED - only the EU host
    # (confirmed, covers India) is trusted. Confirm the exact hostname before routing NA/FE traffic.
    "na": "https://advertising-api.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}
DEFAULT_ADS_REGION = "eu"

GET_LISTING_INCLUDED_DATA = "summaries,attributes,issues,productTypes"
VERIFY_LISTING_INCLUDED_DATA = "issues,summaries"

REPORT_TYPE_BY_ARG = {"sp-keyword": "spTargeting"}
REPORT_COLUMNS = [
    "date", "campaignId", "adGroupId", "keywordId", "keyword", "matchType",
    "impressions", "clicks", "cost", "sales7d", "purchases7d",
]
REPORT_POLL_MAX_ATTEMPTS = 40
REPORT_POLL_INTERVAL_SECONDS = 5

ADS_KEYWORDS_PATH = "/sp/keywords"
ADS_CAMPAIGNS_PATH = "/sp/campaigns"
ADS_KEYWORD_MEDIA_TYPE = "application/vnd.spKeyword.v3+json"
ADS_CAMPAIGN_MEDIA_TYPE = "application/vnd.spCampaign.v3+json"
ADS_BATCH_CHUNK_SIZE = 100  # ponytail: v3 batch max is UNVERIFIED (v2 was 1000 kw / 100 campaigns
# per AMAZON.md); 100 is a deliberately conservative constant, not a confirmed limit - raise only
# after confirming against the vendor OpenAPI spec.
FLIPKART_BATCH_CHUNK_SIZE = 10

AMAZON_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
FLIPKART_RETRYABLE_STATUSES = {500, 503, 599}
MAX_RETRY_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 20.0
DEFAULT_TIMEOUT = 30
FLIPKART_TIMEOUT = 15  # contract requires >= 10s for Flipkart listing APIs

TOKEN_REFRESH_FRACTION = 0.8
SECRET_HEADER_NAMES = {"authorization", "x-amz-access-token",
                       "amazon-advertising-api-clientid"}

WARN_DAYS = 14


# ---------------------------------------------------------------------------
# small generic helpers
# ---------------------------------------------------------------------------

def require_env(var_name):
    val = os.environ.get(var_name)
    if not val:
        raise CliError(f"missing required environment variable: {var_name}")
    return val


def redact_headers(headers):
    redacted = {}
    for k, v in (headers or {}).items():
        redacted[k] = "***REDACTED***" if k.lower() in SECRET_HEADER_NAMES else v
    return redacted


def backoff_base(attempt, base=BACKOFF_BASE_SECONDS, cap=BACKOFF_CAP_SECONDS):
    return min(base * (2 ** attempt), cap)


def backoff_delay(attempt, base=BACKOFF_BASE_SECONDS, cap=BACKOFF_CAP_SECONDS):
    raw = backoff_base(attempt, base, cap)
    return raw * (0.5 + random.random() * 0.5)


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _safe_json(resp):
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw_text": resp.text[:1000]}


def redact_url(url):
    """Query strings can carry credentials (presigned S3 signatures, OAuth params).
    Never log anything after the '?'."""
    base, sep, _ = str(url).partition("?")
    return base + "?<redacted>" if sep else base


def print_dry_run(method, url, headers, body, params=None):
    payload = {
        "dry_run": True,
        "method": method,
        "url": url,
        "params": params or {},
        "headers": redact_headers(headers),
        "body": body,
    }
    print(json.dumps(payload, indent=2))
    return payload


def record_mutation(*, kind, channel, entity_id, before, after, reason, dry_run, run_id_val=None):
    return mp_state.record(
        kind=kind, channel=channel, entity_id=entity_id, before=before, after=after,
        reason=reason, actor="mp_api.py", dry_run=dry_run, run_id=run_id_val,
    )


def http_request(method, url, *, headers=None, params=None, json_body=None, data=None, auth=None,
                  timeout=DEFAULT_TIMEOUT, retryable_statuses=(), max_attempts=MAX_RETRY_ATTEMPTS):
    attempt = 0
    while True:
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_body,
                                     data=data, auth=auth, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            attempt += 1
            if attempt >= max_attempts:
                raise CliError(f"{method} {redact_url(url)} failed after {attempt} attempts: {exc}")
            time.sleep(backoff_delay(attempt))
            continue

        if resp.status_code not in retryable_statuses:
            return resp
        attempt += 1
        if attempt >= max_attempts:
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = None
        if retry_after:
            try:
                delay = min(float(retry_after), BACKOFF_CAP_SECONDS)
            except ValueError:
                delay = None
        if delay is None:
            delay = backoff_delay(attempt)
        print(f"retrying {method} {redact_url(url)} (attempt {attempt}/{max_attempts}), "
              f"status {resp.status_code}, sleeping {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)


def load_items(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise CliError(f"{path}: expected a JSON list, or an object with an 'items' list")


# ---------------------------------------------------------------------------
# config (config/skus.json)
# ---------------------------------------------------------------------------

def load_config():
    path = mp_state.project_root() / "config" / "skus.json"
    if not path.exists():
        raise CliError(f"config file not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CliError(f"config file is not valid JSON: {path} ({exc})")


def find_sku_config(sku):
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    for entry in cfg.get("skus", []):
        if entry.get("sku") == sku:
            merged = dict(defaults)
            merged.update(entry)
            return merged
    raise CliError(f"sku '{sku}' not found in config/skus.json")


# ---------------------------------------------------------------------------
# token cache (never printed, never logged)
# ---------------------------------------------------------------------------

def _cache_dir():
    d = mp_state.project_root() / ".mp-cache"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _cache_path(name):
    return _cache_dir() / f"{name}.json"


def load_token_cache(name):
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_token_cache(name, access_token, expires_in):
    now = time.time()
    data = {
        "access_token": access_token,
        "issued_at": now,
        "expires_in": expires_in,
        "expires_at": now + expires_in,
        "refresh_at": now + expires_in * TOKEN_REFRESH_FRACTION,
    }
    p = _cache_path(name)
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.chmod(p, 0o600)
    return data


def token_needs_refresh(cache):
    if not cache or "refresh_at" not in cache:
        return True
    return time.time() >= cache["refresh_at"]


def get_access_token(cache_name, fetch_fn):
    cache = load_token_cache(cache_name)
    if not token_needs_refresh(cache):
        return cache["access_token"]
    access_token, expires_in = fetch_fn()
    save_token_cache(cache_name, access_token, expires_in)
    return access_token


def fetch_lwa_token(client_id_var, client_secret_var, refresh_token_var):
    def _fetch():
        client_id = require_env(client_id_var)
        client_secret = require_env(client_secret_var)
        refresh_token = require_env(refresh_token_var)
        resp = http_request(
            "POST", LWA_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            retryable_statuses=AMAZON_RETRYABLE_STATUSES,
        )
        if resp.status_code >= 400:
            raise CliError(f"LWA token request failed: HTTP {resp.status_code}")
        body = _safe_json(resp)
        return body["access_token"], body.get("expires_in", 3600)
    return _fetch


def fetch_flipkart_token():
    app_id = require_env(ENV_VARS["flipkart_app_id"])
    app_secret = require_env(ENV_VARS["flipkart_app_secret"])
    resp = http_request(
        "GET", FLIPKART_TOKEN_URL,
        params={"grant_type": "client_credentials", "scope": "Seller_Api"},
        auth=(app_id, app_secret),
        retryable_statuses=FLIPKART_RETRYABLE_STATUSES,
        timeout=FLIPKART_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise CliError(f"Flipkart token request failed: HTTP {resp.status_code}")
    body = _safe_json(resp)
    if "expires_in" not in body:
        # NEVER hardcode a TTL - FLIPKART.md says the docs' own examples disagree.
        raise CliError("Flipkart token response missing 'expires_in'")
    return body["access_token"], body["expires_in"]


# ---------------------------------------------------------------------------
# Amazon SP-API
# ---------------------------------------------------------------------------

def sp_host_for_marketplace(marketplace_id):
    region = MARKETPLACE_TO_SP_REGION.get(marketplace_id, DEFAULT_SP_REGION)
    host = SP_API_HOSTS.get(region)
    if not host:
        raise CliError(f"no SP-API host configured for region '{region}'")
    return host


def _amazon_sp_access_token():
    return get_access_token("amazon_sp", fetch_lwa_token(
        ENV_VARS["amazon_lwa_client_id"], ENV_VARS["amazon_lwa_client_secret"],
        ENV_VARS["amazon_lwa_refresh_token"]))


def cmd_amazon_sp_get_listing(args):
    sku_cfg = find_sku_config(args.sku)
    marketplace_id = sku_cfg.get("marketplace_id")
    if not marketplace_id:
        raise CliError(f"sku '{args.sku}' has no marketplace_id in config/skus.json")
    seller_id = require_env(ENV_VARS["amazon_sp_seller_id"])
    host = sp_host_for_marketplace(marketplace_id)
    access_token = _amazon_sp_access_token()
    headers = {"x-amz-access-token": access_token, "user-agent": USER_AGENT}
    url = f"{host}/listings/2021-08-01/items/{seller_id}/{args.sku}"
    params = {"marketplaceIds": marketplace_id, "includedData": GET_LISTING_INCLUDED_DATA}
    resp = http_request("GET", url, headers=headers, params=params,
                         retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
    print(json.dumps(_safe_json(resp)))
    return 0 if resp.status_code < 400 else 1


def cmd_amazon_sp_verify_listing(args):
    # There is NO submission-status endpoint for Listings Items v2021-08-01; re-GET is the only way
    # to confirm a patch actually took (ACCEPTED from patch-listing is not final).
    sku_cfg = find_sku_config(args.sku)
    marketplace_id = sku_cfg.get("marketplace_id")
    if not marketplace_id:
        raise CliError(f"sku '{args.sku}' has no marketplace_id in config/skus.json")
    seller_id = require_env(ENV_VARS["amazon_sp_seller_id"])
    host = sp_host_for_marketplace(marketplace_id)
    access_token = _amazon_sp_access_token()
    headers = {"x-amz-access-token": access_token, "user-agent": USER_AGENT}
    url = f"{host}/listings/2021-08-01/items/{seller_id}/{args.sku}"
    params = {"marketplaceIds": marketplace_id, "includedData": VERIFY_LISTING_INCLUDED_DATA}
    resp = http_request("GET", url, headers=headers, params=params,
                         retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
    print(json.dumps(_safe_json(resp)))
    return 0 if resp.status_code < 400 else 1


def build_amazon_patch_request(sku, seller_id, marketplace_id, host, patch_payload, dry_run):
    url = f"{host}/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": marketplace_id}
    if dry_run:
        # dry-run stays fully offline (see module docstring / CliError-free path below); this
        # mirrors Amazon's own VALIDATION_PREVIEW mode in the *previewed* params only, it is not
        # actually sent - "get it right" for --dry-run means no network call, full stop.
        params["mode"] = "VALIDATION_PREVIEW"
    headers = {"x-amz-access-token": "<token>", "Content-Type": "application/json",
               "user-agent": USER_AGENT}
    body = {k: v for k, v in patch_payload.items() if k not in ("before", "reason")}
    return "PATCH", url, params, headers, body


def cmd_amazon_sp_patch_listing(args):
    sku_cfg = find_sku_config(args.sku)
    marketplace_id = sku_cfg.get("marketplace_id")
    if not marketplace_id:
        raise CliError(f"sku '{args.sku}' has no marketplace_id in config/skus.json")
    seller_id = require_env(ENV_VARS["amazon_sp_seller_id"])
    host = sp_host_for_marketplace(marketplace_id)

    patch_payload = json.loads(Path(args.patch_file).read_text())
    if "productType" not in patch_payload:
        raise CliError(f"{args.patch_file}: missing required top-level 'productType'")
    if "patches" not in patch_payload:
        raise CliError(f"{args.patch_file}: missing required top-level 'patches'")

    method, url, params, headers_template, body = build_amazon_patch_request(
        args.sku, seller_id, marketplace_id, host, patch_payload, args.dry_run)
    run = mp_state.run_id()

    if args.dry_run:
        print_dry_run(method, url, headers_template, body, params=params)
        record_mutation(kind="listing_patches", channel="amazon", entity_id=args.sku,
                         before=patch_payload.get("before"), after=body.get("patches"),
                         reason=patch_payload.get("reason"), dry_run=True, run_id_val=run)
        return 0

    headers = dict(headers_template)
    headers["x-amz-access-token"] = _amazon_sp_access_token()
    resp = http_request(method, url, headers=headers, params=params, json_body=body,
                         retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
    parsed = _safe_json(resp)
    print(json.dumps(parsed))
    # Only an accepted write may be recorded: a failed write must not start a cooldown
    # clock or produce a rollback entry for a change that never happened.
    accepted = resp.status_code < 400 and (
        not isinstance(parsed, dict) or parsed.get("status") != "INVALID")
    if accepted:
        record_mutation(kind="listing_patches", channel="amazon", entity_id=args.sku,
                         before=patch_payload.get("before"), after=body.get("patches"),
                         reason=patch_payload.get("reason"), dry_run=False, run_id_val=run)
    return 0 if accepted else 1


# ---------------------------------------------------------------------------
# Amazon Ads API
# ---------------------------------------------------------------------------

def ads_host():
    return ADS_API_HOSTS[DEFAULT_ADS_REGION]


def _amazon_ads_access_token():
    return get_access_token("amazon_ads", fetch_lwa_token(
        ENV_VARS["amazon_ads_client_id"], ENV_VARS["amazon_ads_client_secret"],
        ENV_VARS["amazon_ads_refresh_token"]))


def cmd_amazon_ads_report(args):
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=args.days)
    report_type_id = REPORT_TYPE_BY_ARG[args.type]

    body = {
        "name": f"{args.type}-{args.time_unit.lower()}-{args.days}d",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["targeting"],
            "columns": REPORT_COLUMNS,
            "reportTypeId": report_type_id,
            "timeUnit": args.time_unit,
            "format": "GZIP_JSON",
        },
    }

    client_id = require_env(ENV_VARS["amazon_ads_client_id"])
    profile_id = require_env(ENV_VARS["amazon_ads_profile_id"])
    access_token = _amazon_ads_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": client_id,
        "Amazon-Advertising-API-Scope": profile_id,
        "Content-Type": "application/json",
    }
    host = ads_host()

    create_resp = http_request("POST", f"{host}/reporting/reports", headers=headers, json_body=body,
                                retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
    if create_resp.status_code >= 400:
        raise CliError(f"report creation failed: HTTP {create_resp.status_code}: "
                        f"{create_resp.text[:300]}")
    report_id = create_resp.json().get("reportId")
    if not report_id:
        raise CliError("report creation response missing 'reportId'")

    status_url = f"{host}/reporting/reports/{report_id}"
    presigned_url = None
    for _ in range(REPORT_POLL_MAX_ATTEMPTS):
        poll_resp = http_request("GET", status_url, headers=headers,
                                  retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
        if poll_resp.status_code >= 400:
            raise CliError(f"report polling failed: HTTP {poll_resp.status_code}")
        poll_body = poll_resp.json()
        status = poll_body.get("status")
        # ponytail: AMAZON.md marks the exact status enum spelling UNVERIFIED
        # (PENDING -> PROCESSING -> COMPLETED | FAILED assumed). Treat anything that isn't the
        # confirmed failure/URL signal as "still processing" rather than crash on a new spelling.
        if poll_body.get("url"):
            presigned_url = poll_body["url"]
            break
        if status == "FAILED":
            raise CliError(f"report generation FAILED: {poll_body}")
        time.sleep(REPORT_POLL_INTERVAL_SECONDS)

    if not presigned_url:
        raise CliError(f"report {report_id} did not complete within the polling window")

    # Do NOT forward the Ads bearer token to the presigned S3 download - S3 rejects it.
    s3_resp = http_request("GET", presigned_url, headers=None,
                            retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
    if s3_resp.status_code >= 400:
        raise CliError(f"report download failed: HTTP {s3_resp.status_code}")
    rows = json.loads(gzip.decompress(s3_resp.content))

    if args.campaign_ids:
        wanted = set(args.campaign_ids.split(","))
        # ponytail: no confirmed request-level campaign filter shape for reporting v3 exists in
        # AMAZON.md, so we filter the downloaded rows client-side instead of guessing an unverified
        # request field - safe, if less efficient than a server-side filter would be.
        rows = [r for r in rows if str(r.get("campaignId")) in wanted]

    print(json.dumps({"report_id": report_id, "rows": rows}))
    return 0


def _ads_multi_status_ok(parsed):
    # ponytail: the Ads v3 multi-status response shape is UNVERIFIED (AMAZON.md). This is a
    # best-effort heuristic scan for a top-level list of {"code": ...} entries - confirm against a
    # real response and replace with an exact parser before relying on this for auto-decisions.
    if not isinstance(parsed, dict):
        return True
    for value in parsed.values():
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "code" in entry and entry["code"] not in ("SUCCESS", "200", 200):
                    return False
    return True


def build_ads_write_body(wrapper_key, items):
    # ponytail: AMAZON.md UNVERIFIED - v3 write body might be a bare array instead of this
    # {wrapper_key: [...]} wrapping. Confirm against the vendor OpenAPI spec before a real call; if
    # bare-array turns out correct, drop the wrapper_key layer here.
    entities = [{k: v for k, v in item.items() if k not in ("before", "reason")} for item in items]
    return {wrapper_key: entities}


def _ads_write(kind_name, id_field, wrapper_key, url_path, media_type, args):
    items = load_items(args.file)
    if not items:
        raise CliError(f"{args.file}: no items found")
    for item in items:
        if id_field not in item:
            raise CliError(f"{args.file}: item missing required '{id_field}' field")

    run = mp_state.run_id()
    url = f"{ads_host()}{url_path}"
    headers_template = {
        "Authorization": "Bearer <token>",
        "Amazon-Advertising-API-ClientId": "<client-id>",
        "Amazon-Advertising-API-Scope": "<profile-id>",
        "Content-Type": media_type,
        "Accept": media_type,
    }
    chunks = list(chunk_list(items, ADS_BATCH_CHUNK_SIZE))

    if args.dry_run:
        for chunk in chunks:
            print_dry_run("PUT", url, headers_template, build_ads_write_body(wrapper_key, chunk))
        for item in items:
            record_mutation(kind=kind_name, channel="amazon", entity_id=item[id_field],
                             before=item.get("before"),
                             after={k: v for k, v in item.items() if k not in ("before", "reason", id_field)},
                             reason=item.get("reason"), dry_run=True, run_id_val=run)
        return 0

    client_id = require_env(ENV_VARS["amazon_ads_client_id"])
    profile_id = require_env(ENV_VARS["amazon_ads_profile_id"])
    headers = dict(headers_template)
    headers["Authorization"] = f"Bearer {_amazon_ads_access_token()}"
    headers["Amazon-Advertising-API-ClientId"] = client_id
    headers["Amazon-Advertising-API-Scope"] = profile_id

    chunk_results = []
    overall_ok = True
    for chunk in chunks:
        body = build_ads_write_body(wrapper_key, chunk)
        resp = http_request("PUT", url, headers=headers, json_body=body,
                             retryable_statuses=AMAZON_RETRYABLE_STATUSES, timeout=DEFAULT_TIMEOUT)
        parsed = _safe_json(resp)
        chunk_ok = resp.status_code < 400 and _ads_multi_status_ok(parsed)
        if not chunk_ok:
            overall_ok = False
        chunk_results.append({"status_code": resp.status_code, "response": parsed,
                              "recorded": chunk_ok})
        # Do not record a chunk the vendor rejected: cooldown and rollback both read
        # this log and must only ever see changes that actually landed.
        for item in (chunk if chunk_ok else []):
            record_mutation(kind=kind_name, channel="amazon", entity_id=item[id_field],
                             before=item.get("before"),
                             after={k: v for k, v in item.items() if k not in ("before", "reason", id_field)},
                             reason=item.get("reason"), dry_run=False, run_id_val=run)

    print(json.dumps({"run_id": run, "chunks": chunk_results}))
    return 0 if overall_ok else 1


def cmd_amazon_ads_update_bids(args):
    return _ads_write("bids", "keywordId", "keywords", ADS_KEYWORDS_PATH, ADS_KEYWORD_MEDIA_TYPE, args)


def cmd_amazon_ads_update_budgets(args):
    return _ads_write("budgets", "campaignId", "campaigns", ADS_CAMPAIGNS_PATH, ADS_CAMPAIGN_MEDIA_TYPE, args)


# ---------------------------------------------------------------------------
# Flipkart Seller API
# ---------------------------------------------------------------------------

def _flipkart_access_token():
    return get_access_token("flipkart", fetch_flipkart_token)


def cmd_flipkart_get_listings(args):
    skus = [s.strip() for s in args.skus.split(",") if s.strip()]
    if not skus:
        raise CliError("--skus must contain at least one SKU")

    access_token = _flipkart_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    url = f"{FLIPKART_HOST}{FLIPKART_DETAILS_PATH}"

    merged = {"available": {}, "unavailable": [], "invalid": []}
    ok = True
    for chunk in chunk_list(skus, FLIPKART_BATCH_CHUNK_SIZE):
        resp = http_request("POST", url, headers=headers, json_body={"sku_ids": chunk},
                             retryable_statuses=FLIPKART_RETRYABLE_STATUSES, timeout=FLIPKART_TIMEOUT)
        if resp.status_code >= 400:
            ok = False
            merged.setdefault("errors", []).append(
                {"chunk": chunk, "status_code": resp.status_code, "response": _safe_json(resp)})
            continue
        body = _safe_json(resp)
        # NOTE: keys are available/unavailable/invalid - NOT invalid_skus/inactive_listings
        # (FLIPKART.md: the original skill's response keys were wrong).
        merged["available"].update(body.get("available", {}))
        merged["unavailable"].extend(body.get("unavailable", []))
        merged["invalid"].extend(body.get("invalid", []))

    print(json.dumps(merged))
    return 0 if ok else 1


def _flipkart_price_body(items):
    # ponytail: FLIPKART.md marks UNVERIFIED whether the price object's selling-price key is
    # "selling_price" (newer OpenAPI hub) or "sellingPrice" (older docs). We pass the caller's
    # "price" object through verbatim - confirm the correct spelling with one real update/price
    # call before trusting either spelling in the input JSON files.
    return {item["sku"]: {"product_id": item["product_id"], "price": item["price"]} for item in items}


def _flipkart_inventory_body(items):
    return {item["sku"]: {"product_id": item["product_id"], "locations": item["locations"]}
            for item in items}


def build_flipkart_price_request(items):
    headers = {"Authorization": "Bearer <token>", "Content-Type": "application/json"}
    return ("POST", f"{FLIPKART_HOST}{FLIPKART_UPDATE_PRICE_PATH}", {}, headers,
            _flipkart_price_body(items))


def _flipkart_all_success(parsed):
    if not isinstance(parsed, dict):
        return True
    return all(v.get("status") == "SUCCESS" for v in parsed.values() if isinstance(v, dict))


def _flipkart_write(kind_name, url_path, body_fn, args):
    items = load_items(args.file)
    if not items:
        raise CliError(f"{args.file}: no items found")
    for item in items:
        if "sku" not in item:
            raise CliError(f"{args.file}: item missing required 'sku' field")

    run = mp_state.run_id()
    url = f"{FLIPKART_HOST}{url_path}"
    headers_template = {"Authorization": "Bearer <token>", "Content-Type": "application/json"}
    chunks = list(chunk_list(items, FLIPKART_BATCH_CHUNK_SIZE))

    if args.dry_run:
        for chunk in chunks:
            print_dry_run("POST", url, headers_template, body_fn(chunk))
        for item in items:
            record_mutation(kind=kind_name, channel="flipkart", entity_id=item["sku"],
                             before=item.get("before"),
                             after={k: v for k, v in item.items() if k not in ("before", "reason", "sku")},
                             reason=item.get("reason"), dry_run=True, run_id_val=run)
        return 0

    headers = dict(headers_template)
    headers["Authorization"] = f"Bearer {_flipkart_access_token()}"

    merged = {}
    overall_ok = True
    for chunk in chunks:
        body = body_fn(chunk)
        resp = http_request("POST", url, headers=headers, json_body=body,
                             retryable_statuses=FLIPKART_RETRYABLE_STATUSES, timeout=FLIPKART_TIMEOUT)
        parsed = _safe_json(resp)
        if resp.status_code >= 400 or not isinstance(parsed, dict):
            overall_ok = False
        else:
            merged.update(parsed)
            if not _flipkart_all_success(parsed):
                overall_ok = False
        for item in chunk:
            sku_status = parsed.get(item["sku"], {}).get("status") if isinstance(parsed, dict) else None
            if sku_status != "SUCCESS":
                continue   # only SUCCESS landed; FAILURE/WARNING must not enter the audit log
            record_mutation(kind=kind_name, channel="flipkart", entity_id=item["sku"],
                             before=item.get("before"),
                             after={k: v for k, v in item.items() if k not in ("before", "reason", "sku")},
                             reason=item.get("reason"), dry_run=False, run_id_val=run)

    print(json.dumps({"run_id": run, "results": merged}))
    return 0 if overall_ok else 1


def cmd_flipkart_update_price(args):
    return _flipkart_write("price", FLIPKART_UPDATE_PRICE_PATH, _flipkart_price_body, args)


def cmd_flipkart_update_inventory(args):
    return _flipkart_write("inventory", FLIPKART_UPDATE_INVENTORY_PATH, _flipkart_inventory_body, args)


# ---------------------------------------------------------------------------
# credential health check (no writes; Amazon does a live LWA refresh, Flipkart mints a token)
# ---------------------------------------------------------------------------

def _missing_env(*names):
    return [n for n in names if not os.environ.get(n)]


def _status_from_days(days):
    if days <= 0:
        return "expired"
    if days < WARN_DAYS:
        return "warn"
    return "ok"


def check_amazon_credentials():
    result = {"channel": "amazon", "configured": False, "missing_env": [],
              "token_ok": False, "refresh_expires_in_days": None,
              "status": "unconfigured", "detail": ""}
    missing = _missing_env(ENV_VARS["amazon_lwa_client_id"], ENV_VARS["amazon_lwa_client_secret"],
                            ENV_VARS["amazon_lwa_refresh_token"])
    result["missing_env"] = missing
    if missing:
        result["detail"] = "missing required environment variables"
        return result
    result["configured"] = True
    try:
        resp = http_request(
            "POST", LWA_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": os.environ[ENV_VARS["amazon_lwa_refresh_token"]],
                "client_id": os.environ[ENV_VARS["amazon_lwa_client_id"]],
                "client_secret": os.environ[ENV_VARS["amazon_lwa_client_secret"]],
            },
            retryable_statuses=AMAZON_RETRYABLE_STATUSES,
        )
    except CliError as exc:
        result["status"] = "unknown"
        result["detail"] = f"LWA refresh network error: {exc}"
        return result
    if resp.status_code < 400:
        result["token_ok"] = True
        result["status"] = "ok"
        result["detail"] = "LWA refresh succeeded; Amazon publishes no fixed refresh-token TTL"
    elif resp.status_code < 500:
        result["status"] = "expired"
        result["detail"] = f"LWA refresh failed: HTTP {resp.status_code} (refresh token likely revoked)"
    else:
        result["status"] = "unknown"
        result["detail"] = f"LWA refresh failed: HTTP {resp.status_code} (server-side, not conclusive)"
    return result


def check_flipkart_credentials():
    result = {"channel": "flipkart", "configured": False, "missing_env": [],
              "token_ok": False, "refresh_expires_in_days": None,
              "status": "unconfigured", "detail": ""}
    missing = _missing_env(ENV_VARS["flipkart_app_id"], ENV_VARS["flipkart_app_secret"])
    result["missing_env"] = missing
    if missing:
        result["detail"] = "missing required environment variables"
        return result
    result["configured"] = True
    try:
        resp = http_request(
            "GET", FLIPKART_TOKEN_URL,
            params={"grant_type": "client_credentials", "scope": "Seller_Api"},
            auth=(os.environ[ENV_VARS["flipkart_app_id"]], os.environ[ENV_VARS["flipkart_app_secret"]]),
            retryable_statuses=FLIPKART_RETRYABLE_STATUSES,
            timeout=FLIPKART_TIMEOUT,
        )
    except CliError as exc:
        result["status"] = "unknown"
        result["detail"] = f"Flipkart token mint network error: {exc}"
        return result
    if resp.status_code >= 500:
        result["status"] = "unknown"
        result["detail"] = f"Flipkart token mint failed: HTTP {resp.status_code} (server-side, not conclusive)"
        return result
    if resp.status_code >= 400:
        result["status"] = "expired"
        result["detail"] = f"Flipkart token mint failed: HTTP {resp.status_code} (app credentials likely invalid/revoked)"
        return result
    result["token_ok"] = True
    body = _safe_json(resp)
    expires_in = body.get("expires_in")
    # ponytail: this app only ever does the client_credentials grant (no refresh_token is ever
    # issued/stored - see ENV_VARS), so the documented refresh-token expiry endpoint
    # (GET /oauth-service/oauth/token/expiry?token_type=refresh&token=<v>) has nothing to check.
    # FLIPKART.md itself marks refresh-token expiry semantics unverified. Falling back to the
    # access token's own expires_in as a channel-health proxy, not a true refresh-token TTL.
    # Upgrade: if an authorization_code/refresh_token flow is ever wired up, call the expiry
    # endpoint with token_type=refresh instead and drop this fallback.
    if not isinstance(expires_in, (int, float)):
        result["status"] = "unknown"
        result["detail"] = "Flipkart token mint succeeded but response had no 'expires_in'"
        return result
    days = int(expires_in // 86400)
    result["refresh_expires_in_days"] = days
    result["status"] = _status_from_days(days)
    result["detail"] = (f"no refresh token is stored for this app; reporting the client_credentials "
                         f"access token's expires_in ({expires_in}s) as a channel-health proxy, not a "
                         f"verified refresh-token TTL")
    return result


CHECK_CREDENTIALS_FNS = {"amazon": check_amazon_credentials, "flipkart": check_flipkart_credentials}


def cmd_check_credentials(args):
    channels = ["amazon", "flipkart"] if args.channel == "all" else [args.channel]
    results = [CHECK_CREDENTIALS_FNS[c]() for c in channels]
    for r in results:
        print(json.dumps(r))
    return 0 if all(r["status"] == "ok" for r in results) else 1


# ---------------------------------------------------------------------------
# selfcheck - no network
# ---------------------------------------------------------------------------

def _selfcheck():
    # 1. backoff grows and is bounded
    raw_delays = [backoff_base(a) for a in range(8)]
    assert raw_delays == sorted(raw_delays), f"backoff_base should be non-decreasing: {raw_delays}"
    assert raw_delays[-1] <= BACKOFF_CAP_SECONDS
    assert raw_delays[0] < raw_delays[3] < BACKOFF_CAP_SECONDS
    for attempt in range(8):
        jittered = backoff_delay(attempt)
        assert 0 < jittered <= backoff_base(attempt)

    # 2. redaction helper removes every secret from a header dict
    headers = {"Authorization": "Bearer super-secret-token", "x-amz-access-token": "amz-secret",
               "Content-Type": "application/json"}
    redacted = redact_headers(headers)
    assert redacted["Authorization"] == "***REDACTED***"
    assert redacted["x-amz-access-token"] == "***REDACTED***"
    assert redacted["Content-Type"] == "application/json"
    assert "super-secret-token" not in json.dumps(redacted)
    assert "amz-secret" not in json.dumps(redacted)

    # 3a. dry-run request building - one Amazon patch
    patch_payload = {
        "productType": "SHIRT",
        "patches": [{"op": "replace", "path": "/attributes/main_product_image_locator",
                     "value": [{"media_location": "https://cdn.example.com/x.jpg",
                                "marketplace_id": "A21TJRUUN4KGV"}]}],
        "before": {"main_product_image_locator": "old"},
    }
    method, url, params, p_headers, body = build_amazon_patch_request(
        "DEMO-TSHIRT-BLK-M", "SELLER123", "A21TJRUUN4KGV",
        "https://sellingpartnerapi-eu.amazon.com", patch_payload, dry_run=True)
    assert method == "PATCH"
    assert url == "https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/SELLER123/DEMO-TSHIRT-BLK-M"
    assert params == {"marketplaceIds": "A21TJRUUN4KGV", "mode": "VALIDATION_PREVIEW"}
    assert body["productType"] == "SHIRT"
    assert "before" not in body
    assert redact_headers(p_headers)["x-amz-access-token"] == "***REDACTED***"

    # 3b. dry-run request building - one Flipkart price update
    fk_items = [{"sku": "DEMO-JEANS-BLU-32", "product_id": "FSN123ABCDEFG",
                 "price": {"mrp": 5000, "selling_price": 4500, "currency": "INR"}}]
    fmethod, furl, fparams, fheaders, fbody = build_flipkart_price_request(fk_items)
    assert fmethod == "POST"
    assert furl == "https://api.flipkart.net/sellers/listings/v3/update/price"
    assert fbody == {"DEMO-JEANS-BLU-32": {"product_id": "FSN123ABCDEFG",
                                           "price": {"mrp": 5000, "selling_price": 4500, "currency": "INR"}}}
    assert redact_headers(fheaders)["Authorization"] == "***REDACTED***"

    # 4. Flipkart batching splits 25 SKUs into 3 chunks of <= 10
    fk_batch = [{"sku": f"SKU-{i}"} for i in range(25)]
    chunks = list(chunk_list(fk_batch, FLIPKART_BATCH_CHUNK_SIZE))
    assert len(chunks) == 3, f"expected 3 chunks, got {len(chunks)}"
    assert all(len(c) <= 10 for c in chunks)
    assert sum(len(c) for c in chunks) == 25

    # 5. token cache correctly reports an expired token as needing refresh
    assert token_needs_refresh(None) is True
    assert token_needs_refresh({"access_token": "x", "refresh_at": time.time() - 10}) is True
    assert token_needs_refresh({"access_token": "x", "refresh_at": time.time() + 1000}) is False

    # a rejected write must never enter the audit log: cooldown and rollback both read it
    import tempfile, types as _types
    with tempfile.TemporaryDirectory() as td:
        prev = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = td
        try:
            class _R:
                status_code = 200
                headers = {}
                text = json.dumps({"OK": {"status": "SUCCESS"}, "BAD": {"status": "FAILURE"}})
                content = text.encode()
                def json(self):
                    return {"OK": {"status": "SUCCESS"}, "BAD": {"status": "FAILURE"}}
            real_http, real_tok = http_request, _flipkart_access_token
            globals()["http_request"] = lambda *a, **k: _R()
            globals()["_flipkart_access_token"] = lambda: "tok"
            bf = Path(td) / "b.json"
            bf.write_text(json.dumps([
                {"sku": "OK", "product_id": "F1", "before": {"selling_price": 5}, "price": {"selling_price": 4}},
                {"sku": "BAD", "product_id": "F2", "before": {"selling_price": 9}, "price": {"selling_price": 8}},
            ]))
            rc = cmd_flipkart_update_price(_types.SimpleNamespace(file=str(bf), dry_run=False))
            log = Path(td) / "analytics" / "audit" / "price.jsonl"
            ids = [json.loads(l)["entity_id"] for l in log.read_text().splitlines()] if log.exists() else []
            assert rc != 0, "partial failure must exit non-zero"
            assert ids == ["OK"], f"only the SUCCESS sku may be recorded, got {ids}"
        finally:
            globals()["http_request"], globals()["_flipkart_access_token"] = real_http, real_tok
            if prev is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = prev

    # secrets never reach a log: presigned-S3 signatures live in the query string
    _u = redact_url("https://s3/r.gz?X-Amz-Signature=SECRET&X-Amz-Credential=AKIA1")
    assert "SECRET" not in _u and "AKIA" not in _u, _u
    _h = redact_headers({"Authorization": "Bearer sk", "Amazon-Advertising-API-ClientId": "cid",
                         "x-amz-access-token": "Atza|t", "Content-Type": "application/json"})
    assert _h["Authorization"] != "Bearer sk" and _h["Amazon-Advertising-API-ClientId"] != "cid"
    assert _h["x-amz-access-token"] != "Atza|t" and _h["Content-Type"] == "application/json"

    # 7. check-credentials day-math / threshold mapping, no network
    assert _status_from_days(30) == "ok"
    assert _status_from_days(15) == "ok"
    assert _status_from_days(7) == "warn"
    assert _status_from_days(1) == "warn"
    assert _status_from_days(0) == "expired"
    assert _status_from_days(-5) == "expired"

    # 8. an unconfigured channel reports 'unconfigured', never a traceback
    prev_env = {k: os.environ.pop(k, None) for k in
                (ENV_VARS["amazon_lwa_client_id"], ENV_VARS["amazon_lwa_client_secret"],
                 ENV_VARS["amazon_lwa_refresh_token"], ENV_VARS["flipkart_app_id"],
                 ENV_VARS["flipkart_app_secret"])}
    try:
        amazon_result = check_amazon_credentials()
        flipkart_result = check_flipkart_credentials()
    finally:
        for k, v in prev_env.items():
            if v is not None:
                os.environ[k] = v
    assert amazon_result["configured"] is False
    assert amazon_result["status"] == "unconfigured"
    assert sorted(amazon_result["missing_env"]) == sorted(
        [ENV_VARS["amazon_lwa_client_id"], ENV_VARS["amazon_lwa_client_secret"],
         ENV_VARS["amazon_lwa_refresh_token"]])
    assert flipkart_result["configured"] is False
    assert flipkart_result["status"] == "unconfigured"
    assert sorted(flipkart_result["missing_env"]) == sorted(
        [ENV_VARS["flipkart_app_id"], ENV_VARS["flipkart_app_secret"]])

    print("selfcheck ok")
    return 0


def cmd_selfcheck(_args):
    return _selfcheck()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="mp_api.py")
    sub = p.add_subparsers(dest="vendor", required=True)

    sp = sub.add_parser("amazon-sp")
    sp_sub = sp.add_subparsers(dest="command", required=True)

    g = sp_sub.add_parser("get-listing")
    g.add_argument("--sku", required=True)
    g.set_defaults(func=cmd_amazon_sp_get_listing)

    pt = sp_sub.add_parser("patch-listing")
    pt.add_argument("--sku", required=True)
    pt.add_argument("--patch-file", required=True, dest="patch_file")
    pt.add_argument("--dry-run", action="store_true", dest="dry_run")
    pt.set_defaults(func=cmd_amazon_sp_patch_listing)

    vf = sp_sub.add_parser("verify-listing")
    vf.add_argument("--sku", required=True)
    vf.set_defaults(func=cmd_amazon_sp_verify_listing)

    ads = sub.add_parser("amazon-ads")
    ads_sub = ads.add_subparsers(dest="command", required=True)

    rp = ads_sub.add_parser("report")
    rp.add_argument("--type", required=True, choices=sorted(REPORT_TYPE_BY_ARG))
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--campaign-ids", dest="campaign_ids", default=None)
    rp.add_argument("--time-unit", dest="time_unit", default="SUMMARY", choices=["SUMMARY", "DAILY"])
    rp.set_defaults(func=cmd_amazon_ads_report)

    ub = ads_sub.add_parser("update-bids")
    ub.add_argument("--file", required=True)
    ub.add_argument("--dry-run", action="store_true", dest="dry_run")
    ub.set_defaults(func=cmd_amazon_ads_update_bids)

    ubg = ads_sub.add_parser("update-budgets")
    ubg.add_argument("--file", required=True)
    ubg.add_argument("--dry-run", action="store_true", dest="dry_run")
    ubg.set_defaults(func=cmd_amazon_ads_update_budgets)

    fk = sub.add_parser("flipkart")
    fk_sub = fk.add_subparsers(dest="command", required=True)

    gl = fk_sub.add_parser("get-listings")
    gl.add_argument("--skus", required=True)
    gl.set_defaults(func=cmd_flipkart_get_listings)

    up = fk_sub.add_parser("update-price")
    up.add_argument("--file", required=True)
    up.add_argument("--dry-run", action="store_true", dest="dry_run")
    up.set_defaults(func=cmd_flipkart_update_price)

    ui = fk_sub.add_parser("update-inventory")
    ui.add_argument("--file", required=True)
    ui.add_argument("--dry-run", action="store_true", dest="dry_run")
    ui.set_defaults(func=cmd_flipkart_update_inventory)

    cc = sub.add_parser("check-credentials")
    cc.add_argument("--channel", default="all", choices=["amazon", "flipkart", "all"])
    cc.set_defaults(func=cmd_check_credentials)

    sc = sub.add_parser("selfcheck")
    sc.set_defaults(func=cmd_selfcheck)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(json.dumps({"error": str(exc)}))
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort guard, never dump a traceback
        print(json.dumps({"error": f"unexpected error: {exc}"}))
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
