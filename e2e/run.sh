#!/usr/bin/env bash
# End-to-end check of o3p_mcp with okf-mcp over Streamable HTTP, including
# the Odoo data tools of okf-odoo and permission-scoped SQL (plan steps C8,
# D4 and E4).
#
# Usage: e2e/run.sh [--only 1,2,9,d]
#
# 1. Starts PostgreSQL 17, a second PostgreSQL 17 instance as the SQL sandbox
#    (the addon's scripts/sandbox-init.sh and sandbox-setup.sql), Odoo 20 with
#    pglast (the addon's scripts/Dockerfile.test; one database, dbfilter,
#    proxy_mode, --workers=0) and nginx in front of it with docker compose,
#    project "o3p-e2e", all on the host network (see compose.yaml).
# 2. Installs o3p_mcp and sale; setup.py sets the issuer and MCP URL, creates
#    the introspection credentials and the C8 users; setup_sales.py creates
#    the sales users, customers, products and orders, 30,000 contacts, the
#    API key the okf-odoo producer reads metadata with, configures the SQL
#    sandbox and presses its check button, and records what the ORM gives
#    each user.
# 3. suite.mjs builds an okf project in a temporary directory (a small
#    bundle in its own git repository, the `odoo` bundle the okf-odoo
#    producer generates, the okf-odoo tool pack), starts okf-mcp (two
#    instances: the one under test and a second one with another audience),
#    runs the scenarios as an MCP client would and stops okf-mcp again.
# 4. Removes the stack and its volumes.
#
# E2E_KEEP=1 keeps the stack and the setup files (in e2e/.work); then
# E2E_REUSE=1 runs the scenarios again on that stack without rebuilding it.
# okf-mcp and okf-odoo come from the package.json dependencies (paths to
# checkouts with their dependencies installed); OKF_MCP_DIR overrides okf-mcp.
# Ports: E2E_ODOO_PORT (18069, the issuer), E2E_ODOO_INTERNAL_PORT (18070),
# E2E_DB_PORT (15432), E2E_SANDBOX_PORT (15433), E2E_MCP_PORT (18766),
# E2E_MCP2_PORT (18767). O3P_SQL_PASSWORD sets the sandbox roles' password.
# Exits non-zero when a check fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export E2E_ODOO_PORT=${E2E_ODOO_PORT:-18069}
export E2E_ODOO_INTERNAL_PORT=${E2E_ODOO_INTERNAL_PORT:-18070}
export E2E_DB_PORT=${E2E_DB_PORT:-15432}
export E2E_SANDBOX_PORT=${E2E_SANDBOX_PORT:-15433}
export E2E_MCP_PORT=${E2E_MCP_PORT:-18766}
export E2E_MCP2_PORT=${E2E_MCP2_PORT:-18767}
issuer="http://localhost:${E2E_ODOO_PORT}"
mcp_url="http://localhost:${E2E_MCP_PORT}/mcp"
compose=(docker compose -p o3p-e2e -f "$here/compose.yaml")
work=$(mktemp -d)
kept="$here/.work"

cleanup() {
    if [ "${E2E_KEEP:-0}" = 1 ] || [ "${E2E_REUSE:-0}" = 1 ]; then
        echo "stack kept: E2E_REUSE=1 $0 reruns the scenarios; ${compose[*]} down -v removes it"
    else
        "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf "$kept"
    fi
    rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
seconds() { echo $(( $(date +%s%N) / 1000000 )); }
shell() {
    "${compose[@]}" run --rm --no-deps -T \
        -e E2E_ISSUER="$issuer" -e E2E_MCP_URL="$mcp_url" -e E2E_SANDBOX_PORT \
        odoo odoo shell -d o3p_e2e --no-http --log-level=warn
}

cd "$here"
[ -d node_modules ] || npm install --no-audit --no-fund

if [ "${E2E_REUSE:-0}" = 1 ]; then
    step "Reusing the kept stack"
    cp "$kept/setup.json" "$kept/sales.json" "$work/"
else
    step "Odoo stack (project o3p-e2e)"
    started=$(seconds)
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    "${compose[@]}" build --quiet odoo
    "${compose[@]}" up -d --wait db sandbox
    "${compose[@]}" run --rm --no-deps -T odoo odoo -d o3p_e2e -i o3p_mcp,sale \
        --stop-after-init --no-http --log-level=warn
    echo "installed in $(( $(seconds) - started )) ms"
    shell < setup.py | sed -n 's/^E2E_SETUP //p' > "$work/setup.json"
    [ -s "$work/setup.json" ] || { echo "setup failed"; exit 2; }
    shell < setup_sales.py | sed -n 's/^E2E_SALES //p' > "$work/sales.json"
    [ -s "$work/sales.json" ] || { echo "sales setup failed"; exit 2; }
    "${compose[@]}" up -d --wait odoo proxy
    echo "stack ready in $(( $(seconds) - started )) ms; issuer $issuer, MCP URL $mcp_url"
    if [ "${E2E_KEEP:-0}" = 1 ]; then
        mkdir -p "$kept"
        cp "$work/setup.json" "$work/sales.json" "$kept/"
    fi
fi

step "Scenarios"
node suite.mjs --setup "$work/setup.json" --sales "$work/sales.json" --work "$work" "$@"
