# End-to-end checks

`run.sh` checks `o3p_mcp` together with
[okf-mcp](https://github.com/mfdaves/okf-mcp) serving MCP over Streamable
HTTP and the Odoo data tools of [okf-odoo](https://github.com/mfdaves/okf-odoo),
the way MCP clients use them:

- sign-in through OAuth discovery, dynamic client registration, the Odoo
  login and consent pages, tokens, refresh, revocation and introspection
  (C8);
- the Odoo data tools as users with different rights, compared with what
  Odoo's ORM gives each user, and attempts to read what those rights forbid
  (D4);
- permission-scoped SQL through `odoo_sql` with the same users and attempts
  (E4).

Each check prints one `PASS` or `FAIL` line; the script exits non-zero when
a check fails.

## Requirements

- Linux with Docker and Docker Compose v2; the stack uses host networking.
- Node.js 22 or newer, npm and git.
- Free loopback ports, by default 18069, 18070, 15432, 15433, 18766 and
  18767 (see below).
- okf-mcp and okf-odoo, as checkouts or published versions.

## Running

With checkouts of okf-mcp and okf-odoo whose dependencies are installed
(`npm ci` in each):

```sh
OKF_MCP_DIR=/path/to/okf-mcp OKF_ODOO_DIR=/path/to/okf-odoo e2e/run.sh
```

With published versions, installed at run time into `e2e/.deps`:

```sh
OKF_MCP_VERSION=0.13.0-rc.1 OKF_ODOO_VERSION=0.1.2 e2e/run.sh
```

`--only` selects scenarios (`1`–`9` for C8, `d` for D4, `e` for E4); the
sign-in flow always runs:

```sh
OKF_MCP_DIR=… OKF_ODOO_DIR=… e2e/run.sh --only d,e
```

## What it starts

Docker Compose project `o3p-e2e` (`compose.yaml`): PostgreSQL 17, a second
PostgreSQL 17 instance as the SQL sandbox (set up by
`scripts/sandbox-init.sh` and `scripts/sandbox-setup.sql`), Odoo 20 with
`pglast` (`scripts/Dockerfile.test`) serving one database with `proxy_mode`
and `--workers=0`, and nginx in front of Odoo. `setup.py` and `setup_sales.py`
configure the addon and create the test users and data in `odoo shell`.
okf-mcp runs as local Node.js processes over a temporary okf project. The
stack and its volumes are removed at the end.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `OKF_MCP_DIR` / `OKF_MCP_VERSION` | — | okf-mcp checkout, or published version of `@mfdaves/okf-mcp` |
| `OKF_ODOO_DIR` / `OKF_ODOO_VERSION` | — | okf-odoo checkout, or published version of `@mfdaves/okf-odoo` |
| `E2E_ODOO_PORT` | 18069 | Odoo behind nginx: the issuer `http://localhost:<port>` |
| `E2E_ODOO_INTERNAL_PORT` | 18070 | Odoo itself |
| `E2E_DB_PORT` | 15432 | PostgreSQL for Odoo |
| `E2E_SANDBOX_PORT` | 15433 | PostgreSQL SQL sandbox |
| `E2E_MCP_PORT` | 18766 | okf-mcp under test: `http://localhost:<port>/mcp` |
| `E2E_MCP2_PORT` | 18767 | A second okf-mcp with another token audience |
| `O3P_SQL_PASSWORD` | `o3p-sql-e2e` | Password of the sandbox roles |
| `E2E_KEEP` | 0 | `1` keeps the stack and its setup files in `e2e/.work` |
| `E2E_REUSE` | 0 | `1` reruns the scenarios on a kept stack |

## Lab

`lab/` keeps the stack of `run.sh` running, so that several clients can act
as different Odoo users at the same time, and judges what each of them
received.

```sh
OKF_MCP_VERSION=0.13.0-rc.1 OKF_ODOO_VERSION=0.1.2 e2e/lab/lab.sh up   # stays in the foreground
node e2e/lab/call.mjs --as sp_a odoo_search '{"model":"sale.order","domain":[],"fields":["name"]}'
node e2e/lab/check.mjs
e2e/lab/lab.sh down
```

| File | Purpose |
|---|---|
| `lab.sh` | `up` starts the stack, plants the canaries and serves; `stack` and `serve` are its two halves; `down` removes the stack and `e2e/.work` |
| `plant.py` | Run in `odoo shell`: writes canaries, random strings in places some users must never read (the orders of another salesperson, a hidden model), with the users allowed to see each |
| `up.mjs` | Builds the okf project, starts okf-mcp and signs every test user in through the OAuth flow; the users without the MCP group and the portal user get no token |
| `call.mjs` | One MCP call as a test user: `--as <user>`, then `--list`, `--schema <tool>` or `<tool> '<JSON arguments>'`. Prints the answer and appends the call with its complete answer to `e2e/.work/lab/transcripts/<LAB_RUN>/<agent>.<user>.jsonl` (`--agent` names the caller, `LAB_RUN` the run) |
| `oracle.mjs` | The verdict of a run from its transcripts: `LEAK` when an answer to a user contains a canary, or a secret of the installation, that user may not see; `UNPROVEN` when no allowed user read a canary; else `CLEAN`. Exits non-zero unless clean |
| `check.mjs` | The lab's own check: an owner reads the canary, another salesperson and a hidden model do not give it, six users calling at once each get the orders the ORM gives them, each user's context names that user and totals without a groupby equal the ORM's (both when the tool pack offers `odoo_context`), and the oracle tells the clean run from a leak |
| `users.workflow.js` | A Claude Code workflow: one agent per test user does that user's ordinary work through `call.mjs` (sales questions as two salespeople and their manager, a knowledge note as an editor). Its answers are compared with `sales.json` and the run is judged by `oracle.mjs` |

The test users are those of `setup.py` and `setup_sales.py`: `sp_a` and
`sp_b` (salespeople who see their own orders), `manager`, `d_admin`,
`editor`, `editor2`, `user` and `mcp_only`.
