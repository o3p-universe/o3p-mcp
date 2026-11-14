#!/usr/bin/env bash
# End-to-end check of o3p_mcp with okf-mcp over Streamable HTTP.
#
# Usage: e2e/run.sh [--only 1,2,9]
#
# 1. Starts PostgreSQL 17, Odoo 20 (one database, dbfilter, proxy_mode,
#    --workers=0) and nginx in front of it with docker compose, project
#    "o3p-e2e", all on the host network (see compose.yaml).
# 2. Installs o3p_mcp, then setup.py sets the issuer and MCP URL, creates the
#    introspection credentials and the test users.
# 3. suite.mjs creates a small OKF bundle in a temporary git repository,
#    starts okf-mcp (two instances: the one under test and a second one with
#    another audience), runs the scenarios as an MCP client would and stops
#    okf-mcp again.
# 4. Removes the stack and its volumes (E2E_KEEP=1 keeps it running).
#
# okf-mcp comes from the package.json dependency (a path to an okf-mcp
# checkout with its dependencies installed) or from OKF_MCP_DIR.
# Ports: E2E_ODOO_PORT (18069, the issuer), E2E_ODOO_INTERNAL_PORT (18070),
# E2E_DB_PORT (15432), E2E_MCP_PORT (18766), E2E_MCP2_PORT (18767).
# Exits non-zero when a check fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export E2E_ODOO_PORT=${E2E_ODOO_PORT:-18069}
export E2E_ODOO_INTERNAL_PORT=${E2E_ODOO_INTERNAL_PORT:-18070}
export E2E_DB_PORT=${E2E_DB_PORT:-15432}
export E2E_MCP_PORT=${E2E_MCP_PORT:-18766}
export E2E_MCP2_PORT=${E2E_MCP2_PORT:-18767}
issuer="http://localhost:${E2E_ODOO_PORT}"
mcp_url="http://localhost:${E2E_MCP_PORT}/mcp"
compose=(docker compose -p o3p-e2e -f "$here/compose.yaml")
work=$(mktemp -d)

cleanup() {
    if [ "${E2E_KEEP:-0}" = 1 ]; then
        echo "stack kept: ${compose[*]} down -v"
    else
        "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
seconds() { echo $(( $(date +%s%N) / 1000000 )); }

cd "$here"
[ -d node_modules ] || npm install --no-audit --no-fund

step "Odoo stack (project o3p-e2e)"
started=$(seconds)
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d --wait db
"${compose[@]}" run --rm --no-deps -T odoo odoo -d o3p_e2e -i o3p_mcp \
    --stop-after-init --no-http --log-level=warn
"${compose[@]}" run --rm --no-deps -T \
    -e E2E_ISSUER="$issuer" -e E2E_MCP_URL="$mcp_url" \
    odoo odoo shell -d o3p_e2e --no-http --log-level=warn < setup.py \
    | sed -n 's/^E2E_SETUP //p' > "$work/setup.json"
[ -s "$work/setup.json" ] || { echo "setup failed"; exit 2; }
"${compose[@]}" up -d --wait odoo proxy
echo "stack ready in $(( $(seconds) - started )) ms; issuer $issuer, MCP URL $mcp_url"

step "Scenarios"
node suite.mjs --setup "$work/setup.json" --work "$work" "$@"
