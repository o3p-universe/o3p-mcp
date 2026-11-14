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

The metadata and registration endpoints answer cross-origin requests
(`Access-Control-Allow-Origin: *`) and never create a session.

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

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`.

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and Odoo 20 with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. An optional argument replaces the default test tags
(`/o3p_mcp`).
