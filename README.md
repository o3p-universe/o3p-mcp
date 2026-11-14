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

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`.

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17 and Odoo 20 with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. An optional argument replaces the default test tags
(`/o3p_mcp`).
