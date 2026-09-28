# Amazon access

Two independent ways to connect this plugin to Amazon: SP-API (listings/catalog) and Ads API
(Sponsored Products). Each has a connector path (A) and a direct-HTTPS path (B). They are
equals, not a preferred path with a fallback — pick per marketplace and per operational need.

## Which path should I use?

| | Path A — MCP / Claude Connector | Path B — direct HTTPS refresh token |
|---|---|---|
| Authenticate with | Seller Central OAuth login (SP-API) / LWA Client ID+Secret via "Sign in now" (Ads) | LWA Security Profile + manual OAuth consent, once |
| Credential lives in | Claude's connector store | Your process environment (`AMAZON_LWA_REFRESH_TOKEN`, `AMAZON_ADS_REFRESH_TOKEN`, ...) |
| Plugin uses it for | MCP tools (Selling Partner connector, Ads custom connector) | `scripts/mp_api.py` |
| Setup effort | Minutes, once the owner has granted access | Developer-application approval that can take days |

Start with Path A if the connector is offered for your marketplace — it avoids handling refresh
tokens at all. Use Path B when the connector is not offered for your marketplace, or you need
unattended/headless operation (see [Security rules](#security-rules) and
`README.md`'s [Credentials](../README.md#credentials) section for why: a scheduled run has no
browser to complete an OAuth flow in).

## Path A — MCP / Claude Connectors

### A1. SP-API via the official Amazon Selling Partner connector

**Owner (one-time, in Seller Central):**
1. Add/invite the operator's Amazon user account as a user/secondary user.
2. Grant the roles/access the agent needs.
3. If the account uses Amazon's AI-agent access flow, grant AI-agent access in Seller Central.
4. Do NOT send password, SP-API refresh token, Client ID, or Client Secret to the operator — none
   of that is needed for this path.

**You (connect Claude):**
1. Claude -> Settings -> Connectors.
2. Search "Amazon Selling Partner".
3. Select the official/verified connector -> Connect.
4. A browser opens Amazon Seller Central.
5. Sign in with your own authorized Seller Central account — never the owner's.
6. Review the consent screen -> Allow/Authorize.
7. You're redirected back to Claude; the connector shows Connected.
8. Run a read-only test before anything else: "Show my Amazon seller/listing information."

**Key point:** you are NOT entering a Client ID/Secret here. It is a plain Seller Central OAuth
login. This is the step people find confusing — there is no LWA app to create for this flow.

### A2. Amazon Ads via custom connector

**Owner (one-time):**
1. Confirm the Amazon Ads API application is approved for the account/marketplace.
2. Open the LWA Security Profile used by that Ads API application.
3. Add Claude's MCP OAuth callback URL to it: `https://claude.ai/api/mcp/auth_callback`.
4. Keep the existing localhost callback if the app still uses it — do not remove it without
   checking whether anything else depends on it.
5. Securely provide the LWA Client ID + Client Secret to the operator (see
   [Security rules](#security-rules) — never over chat).

**You (connect Claude):**
1. Claude -> Settings -> Connectors.
2. "+" -> Add custom connector.
3. Enter the official Amazon Ads MCP server URL for your account/region (see the region table in
   `README.md`'s [Amazon Ads MCP server](../README.md#amazon-ads-mcp-server-preferred-transport-for-ads)
   section — never substitute a third-party MCP endpoint when an official one exists).
4. Authentication: "Sign in now".
5. OAuth client: "Use your own OAuth client".
6. Enter the LWA Client ID + Client Secret.
7. Add/Connect, complete Amazon authorization.
8. Run a read-only reporting test before any campaign change.

## Path B — direct HTTPS with refresh tokens

This is what `scripts/mp_api.py` uses. Both refresh tokens are one-time manual work; after that,
the backend exchanges them for access tokens programmatically with no browser involved.

### B1. SP-API refresh token

1. Have a Professional Seller Account in Seller Central.
2. Register as an SP-API Developer and submit the private developer application.
3. Create an LWA Security Profile in the Amazon Developer Console.
4. Add callback URL `http://localhost:3000/callback`.
5. Wait for SP-API developer approval.
6. Seller Central -> Developer Central.
7. Find your application -> Authorize.
8. Complete Seller Central login + MFA/CAPTCHA manually.
9. Amazon displays the refresh token.
10. Store it -> env var `AMAZON_LWA_REFRESH_TOKEN`.

### B2. Ads API refresh token

1. Request Ads API access via the Amazon Ads Partner Network Console.
2. Provide business justification, wait for approval.
3. Create an LWA Security Profile with callback `http://localhost:3000/callback`.
4. Generate the Amazon Ads OAuth consent URL.
5. Open it manually in a browser.
6. Log in to Amazon Ads, complete MFA/CAPTCHA.
7. Click Allow.
8. Amazon redirects to `http://localhost:3000/callback?code=...`.
9. Copy the temporary code from the address bar.
10. Exchange the code at the Ads OAuth token endpoint for the refresh token.
11. Store it -> env var `AMAZON_ADS_REFRESH_TOKEN`.

**Does `localhost:3000` need to be running?** No, not for the one-time manual authorization. If
nothing listens on port 3000 the browser shows a connection error, but the `code=` parameter is
still visible in the address bar and can be copied by hand. Running a tiny listener there just
makes the copy step smoother.

After you have both refresh tokens, the backend exchanges them for access tokens
programmatically — no browser login per request. This is exactly what `mp_api.py` does: access
tokens are cached under `.mp-cache/`, refreshed at 80% of TTL. For the full env var table
(including `AMAZON_LWA_CLIENT_ID`, `AMAZON_SP_SELLER_ID`, `AMAZON_ADS_CLIENT_ID`,
`AMAZON_ADS_PROFILE_ID`, and how they're kept out of the process's own reach) see
`README.md`'s [Credentials](../README.md#credentials) section rather than duplicating it here.

## Responsibility split

| Area | Owner | You |
|---|---|---|
| Seller Central | Grants user/agent access | Use your own authorized login |
| SP-API | Authorizes account/user | Connect the official connector |
| Ads API | Maintains the approved app | Configure the Claude connector |
| LWA | Creates/manages ID + Secret | Enter them securely in Claude |
| OAuth | Configures the callback | Complete Amazon sign-in |
| Testing | Confirms permissions | Run read-only tests |

## Security rules

1. **Never paste Client Secrets or refresh tokens into Claude chat, prompts, CLAUDE.md, source
   code, or Git.** This plugin's PreToolUse hook already blocks the agent from reading or echoing
   these values (env dumps, `.env*`/`*credential*`/`*secret*` files, `.mp-cache`, and any
   `$AMAZON_LWA_*` / `$AMAZON_ADS_*` expansion) — do not defeat that by typing the value into a
   chat message instead.
2. For SP-API, prefer Seller Central OAuth (Path A's connector) over hand-handling a refresh
   token.
3. Share the Ads LWA Client Secret only through an approved secure channel.
4. Start with read-only operations before enabling writes.
5. Use the minimum Seller Central roles and Ads permissions needed.

## Troubleshooting

| Symptom | Cause |
|---|---|
| SP-API login/access denied | Your Seller Central user lacks roles / AI-agent access |
| SP-API connector won't connect | Confirm you picked the OFFICIAL connector; retry Seller Central OAuth |
| Ads "invalid client" | Check Client ID/Secret; confirm the LWA profile matches the approved Ads app |
| Ads redirect/callback error | The Claude callback URL is not registered in the LWA Security Profile |
| Ads "no data" | The Ads app/account lacks access to the marketplace being queried |

## A note on availability

Amazon's own launch post described the Selling Partner connector as "available in beta for
sellers in Amazon's U.S. stores, with international expansion to follow," while the operator's
process doc describes the connect flow with no geographic caveat. This document does not assert
either way: attempt the connection, and if your marketplace isn't offered, fall back to Path B,
which has no such restriction.
