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
`com.example.app:/callback`; fragments, user information and schemes such as
`javascript:`, `data:` or `file:` are refused. Redirect URIs are later
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
  returns `error=access_denied` to the client.

The consent page names the client, the user, the MCP server and the scopes,
and shows where the answer is sent. It cannot be framed. Approving posts the
form back with Odoo's CSRF token and redirects to the client with `code`,
`state` and `iss` (RFC 9207); denying redirects with `error=access_denied`.
Other request errors are redirected to the client with `error`,
`error_description`, `state` and `iss`.

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

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`.

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and Odoo 20 with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. An optional argument replaces the default test tags
(`/o3p_mcp`).
