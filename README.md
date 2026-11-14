# O3P MCP

Odoo 18 Community addon that makes Odoo an OAuth 2.1 authorization server for
an external MCP server. MCP clients sign in with an Odoo account; the MCP
server checks every access token through Odoo's introspection endpoint.

This is the `18.0` branch. The `20.0` branch has the same features for
Odoo 20; the protocol, the introspection answer and the data API are
identical on both. Differences specific to Odoo 18 are listed under
"Odoo 18 notes".

## Groups and scopes

The addon adds an **MCP** application category with two groups, shown on
the user form as one MCP selection:

| Group | Scopes granted |
|---|---|
| MCP / User | `okf.read`, `odoo.read`, `odoo.sql` |
| MCP / Editor (implies User) | the User scopes and `okf.write` |

| Scope | Allows |
|---|---|
| `okf.read` | Any MCP call; required in every token |
| `okf.write` | Live write tools and producer runs on the knowledge base |
| `odoo.read` | Reading Odoo records through the data API, with the user's own rights |
| `odoo.sql` | Reserved for permission-scoped SQL; no operation uses it yet |
Only active internal users hold scopes: archived users, portal and public
users (even when placed in an MCP group) and users without the User group
hold none.

## Clients, codes and tokens

- **Clients** (`o3p.mcp.client`) are MCP clients, public (no secret) or
  confidential (`client_secret_post` / `client_secret_basic`), and the
  resource-server credentials the MCP server uses for introspection.
- **Authorization codes** live 5 minutes and work once.
- **Access tokens** live 60 minutes; **refresh tokens** 30 days from their
  issue. Every refresh rotates the refresh token: the new pair belongs to the
  same family, the previous access token stays valid until it expires, and
  presenting an already used refresh token revokes the whole family. One
  exception covers a client that lost the refresh response: if the same
  client presents the just-used refresh token within 10 seconds and the new
  token has not been used yet, the request is refused with `invalid_grant`
  and nothing is revoked. Authorization codes have no such grace.
- Codes, tokens and client secrets are 256-bit random values. Odoo stores only
  their SHA-256 digests, so a token cannot be read back from the database.
- A token answers as active only while it is unexpired and unrevoked, its
  client is active, its user is active and the user still holds `okf.read`.
  The scopes reported are the granted scopes still allowed by the user's
  current groups.
- A daily scheduled action deletes expired codes, tokens whose refresh
  token has expired, and self-registered clients that have not completed a
  token exchange within 24 hours.

## Endpoints

All URLs are relative to the issuer (see Settings).

| Method and path | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `POST /o3p_mcp/oauth/register` | Dynamic client registration (RFC 7591) |
| `GET`, `POST /o3p_mcp/oauth/authorize` | Sign-in and consent (authorization code with PKCE) |
| `POST /o3p_mcp/oauth/token` | Token endpoint (authorization code and refresh token grants) |
| `POST /o3p_mcp/oauth/introspect` | Token introspection for the MCP server (RFC 7662) |
| `POST /o3p_mcp/oauth/revoke` | Token revocation (RFC 7009) |
| `POST /o3p_mcp/api/v1/<op>` | Data API for the MCP server, read-only, as the signed-in user |

The metadata, registration, token and revocation endpoints answer
cross-origin requests (`Access-Control-Allow-Origin: *`); their preflight
allows the `Authorization`, `Content-Type` and `MCP-Protocol-Version`
request headers, the last one being sent by MCP clients on every request.
Introspection and the data API answer no cross-origin requests. None of the
API endpoints creates a session.

### Client registration

The body is a JSON object with at least `redirect_uris`. Each redirect URI
must be an `https` URL, an `http` URL on a loopback host (`localhost`,
`127.0.0.1`, `[::1]`) or a private-use scheme such as
`com.example.app:/callback`. Hosts must be DNS names or IP addresses;
backslashes, fragments, user information, numeric host names such as
`127.1` and schemes such as `javascript:`, `data:` or `file:` are refused.
The same rules apply to redirect URIs edited by an administrator. Redirect URIs are later
compared exactly, except that an `http` redirect URI on a loopback host may
use any port (RFC 8252 §7.3): scheme, host, path and query must still match.

`token_endpoint_auth_method` may be `none` (public client),
`client_secret_post` or `client_secret_basic` (the RFC 7591 default). Grant
types are `authorization_code` and optionally `refresh_token`; the only
response type is `code`. Confidential clients receive their `client_secret`
once, in the registration response.

Each IPv4 address, or IPv6 /64 network, may register at most 20 clients per
hour; further requests get HTTP 429 with `Retry-After`. A self-registered
client that has not completed a token exchange within 24 hours is deleted.

### Authorization

`GET /o3p_mcp/oauth/authorize` takes the usual OAuth 2.1 parameters:
`response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`,
`code_challenge` with `code_challenge_method=S256`, and `resource`.

- A visitor who is not signed in is sent to the Odoo login page and comes
  back afterwards.
- An unknown or archived client, or a redirect URI that does not match one
  of the client's registered URIs, shows an error page; Odoo never redirects
  to an unregistered URI. Matching is exact apart from the port of loopback
  `http` URIs. When the client has a single redirect URI,
  `redirect_uri` may be omitted.
- PKCE with `S256` is required; `plain` is refused.
- `resource` must be the configured MCP URL (a trailing slash is ignored).
  When it is omitted, the configured MCP URL is used, since it is the only
  resource Odoo issues tokens for.
- `scope` may list any of the four scopes; `okf.read` is always added,
  since every call needs it, and without a known scope all four are
  requested. `offline_access` (asked for by clients that want refresh
  tokens, which are always issued) and unknown scopes are ignored rather
  than refused, as RFC 6749 §3.3 allows. The grant is narrowed to what the
  user's groups allow, so an MCP user who asks for `okf.write` does not get
  it; the token response's `scope` names what was granted. A token only carries the scopes requested when it was authorized:
  a client that asked for `okf.read okf.write` has to sign in again to use
  the Odoo scopes.
- A user without the MCP User group sees an error page with a link that
  returns `error=access_denied` to the client. This is checked right after
  the client and redirect URI, before any other parameter.

The consent page names the client, the user, the MCP server and the scopes,
and shows where the answer is sent: the redirect host in its ASCII (IDNA)
form, so a look-alike Unicode name appears as `xn--…`. It cannot be framed. Approving posts the
form back with Odoo's CSRF token and redirects to the client with `code`,
`state` and `iss` (RFC 9207); denying redirects with `error=access_denied`.
For clients created by an administrator, other request errors are
redirected to the client with `error`, `error_description`, `state` and
`iss`. Self-registered clients are never redirected to before the user has
answered: their request errors show the error page with an explicit
"Return to <host>" link.

### Token endpoint

`POST /o3p_mcp/oauth/token` takes a form-encoded body. Clients authenticate
with the method they registered: `client_id` alone for public clients,
`client_id` and `client_secret` in the body for `client_secret_post`, HTTP
Basic for `client_secret_basic`. Mixing Basic with body credentials is
refused. Responses carry `Cache-Control: no-store`.

- `grant_type=authorization_code` with `code`, `code_verifier`,
  `redirect_uri` (the one used at authorization, under the same matching
  rule; it may be omitted only when the client has a single redirect URI) and optionally `resource`.
  A code is consumed by its first exchange, successful or not; presenting it
  again revokes the tokens it was exchanged for.
- `grant_type=refresh_token` with `refresh_token` and optionally `scope`
  (a subset of the original grant that includes `okf.read`; `offline_access`
  and unknown scopes are ignored, a scope beyond the grant or without
  `okf.read` gets `invalid_scope` and leaves the refresh token usable) and
  `resource`. The user must still be
  active and in the MCP groups: losing the Editor group narrows the new
  token to `okf.read`, losing the User group refuses the refresh and revokes
  the family.

The response is `access_token`, `token_type` (`Bearer`), `expires_in`,
`refresh_token` and `scope`. Errors follow RFC 6749 (`invalid_client` with
HTTP 401, `invalid_grant`, `invalid_scope`, `invalid_target`,
`unsupported_grant_type`).

### Introspection

The MCP server posts `token=<access token>` to
`/o3p_mcp/oauth/introspect`, authenticated with HTTP Basic as a
resource-server client. Missing or wrong credentials get HTTP 401 with
`WWW-Authenticate: Basic`. An active token answers:

```json
{ "active": true, "sub": "42", "username": "jdoe", "name": "Jane Doe",
  "email": "jdoe@example.com", "scope": "okf.read okf.write odoo.read odoo.sql",
  "aud": "https://mcp.example.com/mcp", "exp": 1794000000, "client_id": "c_…" }
```

`sub` is the Odoo user ID, `username` the login, `aud` the configured MCP
URL, `exp` the access token expiry (seconds since the epoch) and `client_id`
the MCP client the token was issued to. `scope` is recomputed from the user's
current groups at every call. `email` is left out when the user has none.
Every other case (unknown, expired, revoked, refresh token, archived user or
client, user without the MCP User group) answers exactly
`{ "active": false }`.

### Revocation

`POST /o3p_mcp/oauth/revoke` with `token` (access or refresh token) and
optionally `token_type_hint`, authenticated like the token endpoint. Revoking
either token revokes every token of the same authorization. Unknown tokens
and tokens of other clients are ignored; the answer is always an empty
HTTP 200 once the client is authenticated.

## Data API

The MCP server reads Odoo data on behalf of a signed-in user through
`POST /o3p_mcp/api/v1/<op>`. It authenticates with HTTP Basic as the
resource-server client (the introspection credentials, form-urlencoded before
base64 as RFC 6749 §2.3.1 requires) and passes the user's access token in the
JSON body:

```json
{ "token": "<the user's access token>", "params": { "model": "res.partner" } }
```

Odoo checks the token as introspection does (unrevoked, unexpired, issued to
an active client for the configured MCP server URL, user active and internal,
not a portal user, `okf.read` still allowed by the groups) and requires the
operation's scope (`odoo.read`). The operation then runs as that user, in the
user's language and time zone: Odoo's access rights, record rules and field
groups decide what the answer contains. It runs in a read-only database
transaction with a time limit, so it cannot change anything. The endpoint
answers no cross-origin requests and never creates a session.

A success is `200 { "result": … }`. An error is
`{ "error": { "code": "…", "message": "…" } }` with a fixed message that
never contains an exception text, a parameter or record data:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_params` | The body or a parameter is not valid |
| 401 | `invalid_client` | The resource-server credentials are missing or wrong |
| 403 | `invalid_token` | The user's token fails any check above |
| 403 | `insufficient_scope` | The token lacks the operation's scope |
| 403 | `access_denied` | The user may not read the model or the records |
| 404 | `unknown_op` | The operation does not exist (`sql` is reserved and not offered yet) |
| 404 | `unknown_model` | The model does not exist, is transient or abstract, or is hidden |
| 413 | `result_too_large` | The answer exceeds the configured size |
| 422 | `query_error` | The database or the ORM refused the query |
| 504 | `timeout` | The query exceeded the time limit |

### Operations

| Operation | Parameters | Result |
|---|---|---|
| `models` | `query` (optional, matched on the technical and the display name), `limit` (1–50, default 50) | `[{ "model", "name" }]`: the models the user may read |
| `describe` | `model` | `{ "model", "name", "fields": [{ "name", "string", "type", "relation"?, "required", "readonly", "selection"?, "help"? }] }`: the fields the user may read |

| `search` | `model`, `domain`, `fields`, `order`?, `limit` (1–200, default 80), `offset`? (0–1000000) | `{ "records", "count", "truncated" }` |
| `read` | `model`, `ids` (1–100), `fields` | `{ "records" }` |
| `group` | `model`, `domain`, `groupby` (1–5), `aggregates` (1–20), `order`?, `limit` (1–500, default 80) | `{ "groups": [{ <groupby and aggregate keys> }] }` |

`group` runs Odoo's grouped read as the user, so counts and totals cover
only the records the user may read; it never adds empty groups. Group
values come as in the web client: `[id, name]` for relations,
`[start, label]` for dates. `models` never lists transient, abstract or
hidden models. Records come in
Odoo's `read` format: `id` plus the requested fields, many-to-one values as
`[id, name]`, dates and datetimes as UTC strings. `count` counts every record
the domain matches for the user; `truncated` tells whether more follow.

Parameters are checked strictly, as the user, and a request with any invalid
part, or a parameter the operation does not take, is refused with
`invalid_params`; nothing is silently dropped:

- `fields` lists field names the user may read and that are not hidden.
- `domain` is a JSON domain list (`["&", ["name", "ilike", "x"], …]`) parsed
  by Odoo's domain machinery. Every field path is checked hop by hop:
  readable and not hidden; every hop but the last a relational field to a
  model the user may read, whose searches apply that model's rules; at most
  4 hops. `any` / `not any` sub-domains are checked against the related
  model. Operators: `=`, `!=`, `>`, `>=`, `<`, `<=`, `=?`, `in`, `not in`,
  `like`, `not like`, `ilike`, `not ilike`, `=like`, `=ilike`, `child_of`,
  `parent_of`, `any`, `not any`. Values are JSON scalars or lists of
  scalars.
- `order` is a comma-separated list of `field`, `field asc` or `field desc`
  on sortable, readable, non-hidden fields.
- `groupby` items are groupable readable fields; date and datetime fields
  need a granularity (`date:month`: `day`, `week`, `month`, `quarter`,
  `year`, and `hour` for datetimes) and other fields take none.
- `aggregates` items are `__count`, `field:sum`, `field:avg`, `field:min` or
  `field:max` on numeric (integer, float, monetary) readable fields, and
  `field:count` or `field:count_distinct` on stored readable fields.
- `group` orders by its own items: `amount_total:sum desc`, `__count desc`,
  `date:month asc`.
- `read` returns the records the user may read, in the order of `ids`; ids
  that do not exist and records the user may not read are left out alike,
  so the answer does not reveal whether a hidden record exists.

### Hidden models and fields

Some data is never exposed, whatever the user's rights, administrators
included. The lists are settings (one pattern per line, `*` matches
anything); an empty list restores the default.

- Models, by default: `ir.config_parameter`, `res.users.apikeys*`,
  `o3p.mcp.*`, `auth_totp*`, `ir.mail_server`, `fetchmail.server`,
  `payment.provider`.
- Field names, on every model, by default: `*password*`, `*secret*`,
  `*token*`, `*api_key*`.
- Always: binary fields (files and images), and relational fields that
  point to a hidden model.

### Call log

Every call that reaches the operation or fails authentication is recorded
in **Settings › Technical › MCP › API Calls**: time, user, MCP client,
operation, model, HTTP status, error code, row count and duration. Parameters
and results are never stored. Rows older than the configured number of days
(default 30) are deleted by the daily scheduled action.

## Settings

**Settings › MCP** (administrators only), stored as `ir.config_parameter`
values under `o3p_mcp.*`:

| Setting | Parameter | Default |
|---|---|---|
| Issuer: public URL of this Odoo as authorization server, https (http only on localhost), no path | `o3p_mcp.issuer` | `web.base.url` at install |
| MCP server URL: the public MCP endpoint, the only resource and token audience | `o3p_mcp.resource_url` | empty |
| Access token lifetime, 1 to 1440 minutes | `o3p_mcp.access_token_minutes` | 60 |
| Refresh token lifetime, 1 to 365 days, counted from each refresh | `o3p_mcp.refresh_token_days` | 30 |
| Data API hidden models, one pattern per line | `o3p_mcp.api_denied_models` | see Data API |
| Data API hidden field names, one pattern per line | `o3p_mcp.api_denied_fields` | see Data API |
| Data API largest answer (KB; stored in bytes) | `o3p_mcp.api_max_response_bytes` | 1000 KB |
| Data API time limit per query (seconds) | `o3p_mcp.api_timeout_seconds` | 15 |
| Data API call log retention (days) | `o3p_mcp.api_log_days` | 30 |

The issuer is used for the metadata, the endpoint URLs and the `iss`
parameter. It does not follow `web.base.url`, which Odoo rewrites whenever
an administrator signs in (unless `web.base.url.freeze` is set), so clients
always see the same authorization server. The section also shows the
metadata URL and the introspection URL, and offers two buttons:

- **Create or rotate credentials** creates the resource-server client the MCP
  server introspects with, or gives it a new secret. The client ID and secret
  appear once, in a notification; the previous secret stops working at once.
- **Check MCP server** calls the `/health` URL next to the MCP endpoint
  (`https://mcp.example.com/mcp` → `https://mcp.example.com/health`) and
  reports the HTTP status.

## Management

**Settings › Technical › MCP** (administrators only):

- **Clients**: registered MCP clients and the resource-server client, with
  their redirect URIs (one per line, same rules as registration),
  authentication method, registration address and last use. Archiving a
  client deactivates its tokens; **Revoke all tokens** revokes them.
- **API Calls**: the data API call log (read-only).
- **Tokens**: issued tokens with user, client, scope, state (active,
  rotated, expired, revoked) and times. **Revoke**, on a token or on a
  selection, revokes every token of the same authorization.

Tokens do not depend on the user's Odoo session: logging out or changing the
password leaves them valid. Removing the user's MCP groups or archiving the
user takes effect at the next introspection; revoking tokens takes effect
at once in Odoo (the MCP server may cache an answer for up to a minute).

## Connecting an MCP client

1. Install the addon and give users the **MCP / User** or **MCP / Editor**
   group.
2. In **Settings › MCP**, check the issuer, enter the MCP server URL and
   create the introspection credentials.
3. Configure the MCP server with Odoo as its authorization server: the
   issuer shown in the settings, the introspection URL, the introspection
   client ID and secret, and a public URL equal to the MCP server URL. With
   okf-mcp:

   ```sh
   OKF_INTROSPECTION_CLIENT_ID=c_… OKF_INTROSPECTION_CLIENT_SECRET=… \
   okf mcp --http --public-url https://mcp.example.com/mcp \
     --authorization-server https://odoo.example.com \
     --auth introspect:https://odoo.example.com/o3p_mcp/oauth/introspect
   ```

4. Add the MCP server URL to the MCP client. The client reads the server's
   protected-resource metadata, discovers Odoo, registers itself, opens the
   Odoo sign-in page and asks the user for consent.

### okf-mcp options

- `--public-url` is mandatory with `--auth introspect:…` and must equal the
  MCP server URL configured in Odoo; one trailing slash is ignored on either
  side. Odoo reports that URL as `aud`, and okf-mcp accepts only tokens whose
  `aud` matches.
- Write tools additionally need `--write --actor <actor>`, and `--git-commit`
  for changes to be committed; the token then needs `okf.write`, which only
  MCP editors get.
- Binding a non-loopback `--host` requires `--auth`.
- The issuer given to `--authorization-server` must be https (or
  `localhost`), and the introspection URL https (http only on a loopback
  host). The issuer setting in Odoo follows the same rule.
- okf-mcp sends the introspection credentials with HTTP Basic,
  form-urlencoding them first as RFC 6749 §2.3.1 requires; Odoo decodes them
  the same way. Client IDs (`c_` and hexadecimal digits) and secrets
  (URL-safe base64 from `secrets.token_urlsafe`) contain only URL-safe
  characters, so the encoding leaves them unchanged.
- okf-mcp answers `GET /health` both at its origin and next to the MCP
  endpoint (`/okf/mcp` → `/okf/health`), so **Check MCP server** works for
  either layout.

## Deployment notes

- The issuer starts as `web.base.url` when the addon is installed; set it
  to the public URL in the settings if that differs.
- Behind a reverse proxy, run Odoo with `proxy_mode` and have the proxy set
  `X-Forwarded-For`, `X-Forwarded-Host` and `X-Forwarded-Proto`. Without
  them every request appears to come from the proxy, and the per-address
  registration limit applies to all clients together.
- The metadata, registration, token, introspection and revocation endpoints
  run without a session, so Odoo must pick the database from the request
  alone: serve a single database or set `dbfilter` so the host name selects
  it.

## Odoo 18 notes

- The groups belong to the **MCP** module category (Odoo 20 groups them
  under an MCP privilege). Access to the addon's own records is granted by
  `ir.model.access` to Settings administrators only.
- Odoo 18 has no light and regular internal users: a user is internal or
  portal. The MCP groups imply no other group, so giving them to a user
  changes neither the user type nor any right on Odoo records.
- The data API applies Odoo 18's access rights (`ir.model.access`), record
  rules (`ir.rule`) and field groups, with the same results as on Odoo 20.
  Group values are formatted as on Odoo 20 (`[id, name]` for relations,
  `[start, label]` for dates).
- The daily clean-up deletes up to 1,000 rows per call; while rows remain,
  Odoo calls it again up to 10 times in the same run, and anything left
  waits for the next day.
- The index used by the registration limit is created when the addon is
  installed and kept as it is on later upgrades.

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`, which picks the branch matching the
Odoo version (`18.0`). The addon depends on `base_setup` and `web` and uses
the Python `requests` and `idna` packages (`idna` comes with `requests`).

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and the Odoo image of the
manifest's series (`odoo:18.0` here) with Docker Compose, installs the addon
in a throw-away database, runs its tests and removes the containers again.
`ODOO_SERIES=20.0` selects another image; the Compose project is
`o3p-mcp-test18` for Odoo 18. An optional argument replaces the default test
tags (`/o3p_mcp`).

`tests/fixtures/contract_20.json` holds the data API error bodies, their fixed
messages and the introspection answers generated from the `20.0` branch; the
tests compare the answers on Odoo 18 with it byte for byte.
