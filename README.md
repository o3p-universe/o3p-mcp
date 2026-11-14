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

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`.

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and Odoo 20 with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. An optional argument replaces the default test tags
(`/o3p_mcp`).
