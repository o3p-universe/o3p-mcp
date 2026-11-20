#!/usr/bin/env bash
# The lab: the end-to-end stack kept running, canaries planted, okf-mcp
# serving with one token per test user (see up.mjs, call.mjs, oracle.mjs).
#
#   e2e/lab/lab.sh stack   # start the stack and plant the canaries; returns
#   e2e/lab/lab.sh writes  # open record writing (writes.py); before serve
#   e2e/lab/lab.sh serve   # okf-mcp and the sign-ins; stays in the foreground
#   e2e/lab/lab.sh up      # stack, then serve
#   e2e/lab/lab.sh down    # remove the stack and e2e/.work
#
# okf-mcp and okf-odoo come from OKF_MCP_DIR / OKF_ODOO_DIR or from
# OKF_MCP_VERSION / OKF_ODOO_VERSION, as for e2e/run.sh.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
e2e=$(dirname "$here")
compose=(docker compose -p o3p-e2e -f "$e2e/compose.yaml")

# The directory run.sh installed a published version into.
installed() {
    local name=$1 version=$2
    printf '%s\n' "$e2e/.deps/${name//[@\/]/_}-$version/node_modules/$name"
}

stack() {
    E2E_KEEP=1 "$e2e/run.sh" --only none
    "${compose[@]}" run --rm --no-deps -T odoo odoo shell -d o3p_e2e --no-http --log-level=warn \
        < "$here/plant.py" 2>/dev/null | sed -n 's/^LAB_PLANT //p' > "$e2e/.work/canaries.json"
    [ -s "$e2e/.work/canaries.json" ] || { echo "planting the canaries failed" >&2; exit 2; }
    echo "canaries planted"
}

# Before serve: the sign-ins then carry the odoo.write scope.
writes() {
    "${compose[@]}" run --rm --no-deps -T odoo odoo shell -d o3p_e2e --no-http --log-level=warn \
        < "$here/writes.py" 2>/dev/null | sed -n 's/^LAB_WRITES //p' > "$e2e/.work/writes.json"
    [ -s "$e2e/.work/writes.json" ] || { echo "opening the writes failed" >&2; exit 2; }
    echo "writes opened: $(cat "$e2e/.work/writes.json")"
}

serve() {
    export OKF_MCP_DIR=${OKF_MCP_DIR:-$(installed @mfdaves/okf-mcp "${OKF_MCP_VERSION:?set OKF_MCP_DIR or OKF_MCP_VERSION}")}
    export OKF_ODOO_DIR=${OKF_ODOO_DIR:-$(installed @mfdaves/okf-odoo "${OKF_ODOO_VERSION:?set OKF_ODOO_DIR or OKF_ODOO_VERSION}")}
    exec node "$here/up.mjs"
}

case "${1:-}" in
    stack) stack ;;
    writes) writes ;;
    serve) serve ;;
    up) stack; serve ;;
    down)
        "${compose[@]}" down -v --remove-orphans
        rm -rf "$e2e/.work"
        ;;
    *) echo "usage: $0 stack|writes|serve|up|down" >&2; exit 2 ;;
esac
