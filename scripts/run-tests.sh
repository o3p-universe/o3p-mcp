#!/usr/bin/env bash
# Install o3p_mcp in a throw-away database and run its tests.
#
# Usage: scripts/run-tests.sh [TEST_TAGS]     (default: /o3p_mcp)
#
# Starts PostgreSQL 17, a second PostgreSQL 17 instance as the SQL sandbox
# and Odoo 20 (an image with pglast added, built on first use) with docker
# compose, runs the tests once and removes the containers and volumes again.
# The compose project is "o3p-mcp-test", or $O3P_MCP_TEST_PROJECT. The test
# database also gets the modules in $O3P_MCP_TEST_MODULES (default
# "calendar,project", used by tests of models that hide data on read and of
# relations that bypass access; set it empty for o3p_mcp alone). Exits
# non-zero when the install or any test fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tags=${1:-/o3p_mcp}
extra=${O3P_MCP_TEST_MODULES-calendar,project}
compose=(docker compose -p "${O3P_MCP_TEST_PROJECT:-o3p-mcp-test}" -f "$here/compose.test.yaml")
log=$(mktemp)

cleanup() {
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$log"
}
trap cleanup EXIT

"${compose[@]}" build --quiet odoo
"${compose[@]}" up -d --wait db sandbox >/dev/null

set +e
"${compose[@]}" run --rm --no-deps odoo odoo \
    --addons-path=/mnt/extra-addons \
    -d o3p_mcp_test -i "o3p_mcp${extra:+,$extra}" \
    --test-enable --test-tags "$tags" \
    --stop-after-init 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
set -e

summary=$(grep -E 'odoo\.tests\.result: ' "$log" | tail -n 1 | sed -E 's/.*odoo\.tests\.result: //' || true)
echo
echo "odoo exit status: $status"
echo "tests: ${summary:-no test summary found}"

if [ "$status" -ne 0 ] || [ -z "$summary" ] || grep -qE ' (ERROR|CRITICAL) ' "$log"; then
    echo "FAILED"
    exit 1
fi
echo "OK"
