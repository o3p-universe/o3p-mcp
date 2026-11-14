# O3P MCP

Odoo 20 Community addon that makes Odoo an OAuth 2.1 authorization server for
an external MCP server. MCP clients sign in with an Odoo account; the MCP
server checks every access token through Odoo's introspection endpoint.

## Groups and scopes

The addon adds an **MCP** privilege with two groups:

| Group | Scopes granted |
|---|---|
| MCP / User | `okf.read` |
| MCP / Editor (implies User) | `okf.read`, `okf.write` |

`okf.read` is required for any MCP call; `okf.write` enables live write tools.
Archived users, the public user and users without the User group hold no
scope.

## Clients, codes and tokens

- **Clients** (`o3p.mcp.client`) are MCP clients, public (no secret) or
  confidential (`client_secret_post` / `client_secret_basic`), and the
  resource-server credentials the MCP server uses for introspection.
- **Authorization codes** live 5 minutes and work once.
- **Access tokens** live 60 minutes; **refresh tokens** 30 days from their
  issue. Every refresh rotates the refresh token: the new pair belongs to the
  same family, the previous access token stays valid until it expires, and
  presenting an already used refresh token revokes the whole family.
- Codes, tokens and client secrets are 256-bit random values. Odoo stores only
  their SHA-256 digests, so a token cannot be read back from the database.
- A token answers as active only while it is unexpired and unrevoked, its
  client is active, its user is active and the user still holds `okf.read`.
  The scopes reported are the granted scopes still allowed by the user's
  current groups.
- A daily scheduled action deletes expired codes and tokens whose refresh
  token has expired.

## Endpoints

All URLs are relative to `web.base.url`, which is also the issuer.

| Method and path | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `POST /o3p_mcp/oauth/register` | Dynamic client registration (RFC 7591) |
| `GET`, `POST /o3p_mcp/oauth/authorize` | Sign-in and consent (authorization code with PKCE) |
| `POST /o3p_mcp/oauth/token` | Token endpoint (authorization code and refresh token grants) |
| `POST /o3p_mcp/oauth/introspect` | Token introspection for the MCP server (RFC 7662) |
| `POST /o3p_mcp/oauth/revoke` | Token revocation (RFC 7009) |

The metadata, registration, token and revocation endpoints answer
cross-origin requests (`Access-Control-Allow-Origin: *`); introspection does
not. None of the API endpoints creates a session.

### Client registration

The body is a JSON object with at least `redirect_uris`. Each redirect URI
must be an `https` URL, an `http` URL on a loopback host (`localhost`,
`127.0.0.1`, `[::1]`) or a private-use scheme such as
`com.example.app:/callback`. Hosts must be DNS names or IP addresses;
backslashes, fragments, user information, numeric host names such as
`127.1` and schemes such as `javascript:`, `data:` or `file:` are refused.
The same rules apply to redirect URIs edited by an administrator. Redirect URIs are later
compared exactly, port included.

`token_endpoint_auth_method` may be `none` (public client),
`client_secret_post` or `client_secret_basic` (the RFC 7591 default). Grant
types are `authorization_code` and optionally `refresh_token`; the only
response type is `code`. Confidential clients receive their `client_secret`
once, in the registration response.

Each address may register at most 20 clients per hour; further requests get
HTTP 429 with `Retry-After`.

### Authorization

`GET /o3p_mcp/oauth/authorize` takes the usual OAuth 2.1 parameters:
`response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`,
`code_challenge` with `code_challenge_method=S256`, and `resource`.

- A visitor who is not signed in is sent to the Odoo login page and comes
  back afterwards.
- An unknown or archived client, or a redirect URI that is not exactly one of
  the client's registered URIs, shows an error page; Odoo never redirects to
  an unregistered URI. When the client has a single redirect URI,
  `redirect_uri` may be omitted.
- PKCE with `S256` is required; `plain` is refused.
- `resource` must be the configured MCP URL (a trailing slash is ignored).
  When it is omitted, the configured MCP URL is used, since it is the only
  resource Odoo issues tokens for.
- `scope` may list `okf.read` and `okf.write` and must include `okf.read`;
  without `scope` both are requested. The grant is narrowed to what the
  user's groups allow, so an MCP user who asks for `okf.write` gets
  `okf.read` only.
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
  `redirect_uri` (exactly the one used at authorization; it may be omitted
  only when the client has a single redirect URI) and optionally `resource`.
  A code is consumed by its first exchange, successful or not; presenting it
  again revokes the tokens it was exchanged for.
- `grant_type=refresh_token` with `refresh_token` and optionally `scope`
  (a subset of the original grant) and `resource`. The user must still be
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
  "email": "jdoe@example.com", "scope": "okf.read okf.write",
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

## Settings

**Settings › MCP** (administrators only), stored as `ir.config_parameter`
values under `o3p_mcp.*`:

| Setting | Parameter | Default |
|---|---|---|
| MCP server URL: the public MCP endpoint, the only resource and token audience | `o3p_mcp.resource_url` | empty |
| Access token lifetime, 1 to 1440 minutes | `o3p_mcp.access_token_minutes` | 60 |
| Refresh token lifetime, 1 to 365 days, counted from each refresh | `o3p_mcp.refresh_token_days` | 30 |

The section also shows the issuer, the metadata URL and the introspection
URL, and offers two buttons:

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
2. In **Settings › MCP**, enter the MCP server URL and create the
   introspection credentials.
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

## Deployment notes

- The issuer is `web.base.url`. Behind a reverse proxy, run Odoo with
  `proxy_mode` so that `web.base.url` is the public URL and the registration
  limit counts the clients' addresses rather than the proxy's.
- The metadata, registration, token, introspection and revocation endpoints
  run without a session, so Odoo must pick the database from the request
  alone: serve a single database or set `dbfilter` so the host name selects
  it.

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`. The addon depends on `base_setup`
and `web` and uses the Python `requests` package.

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and Odoo 20 with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. An optional argument replaces the default test tags
(`/o3p_mcp`).
