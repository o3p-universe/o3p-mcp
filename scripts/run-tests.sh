#!/usr/bin/env bash
# Install o3p_mcp in a throw-away database and run its tests.
#
# Usage: [ODOO_SERIES=18.0] scripts/run-tests.sh [TEST_TAGS]
#        (default tags: /o3p_mcp)
#
# Starts PostgreSQL 17, a second PostgreSQL 17 instance as the SQL sandbox
# and the odoo:<series> image with pglast added (built on first use) with
# docker compose, runs the tests once and removes the containers and volumes
# again. The series is read from the addon's manifest version (18.0.x ->
# odoo:18.0) unless ODOO_SERIES is set. The compose project is
# "o3p-mcp-test" for 20.0 and "o3p-mcp-test<major>" otherwise, or
# $O3P_MCP_TEST_PROJECT. The test database also gets the modules in
# $O3P_MCP_TEST_MODULES (default "calendar,project", used by tests of models
# that hide data on read and of relations that bypass access; set it empty
# for o3p_mcp alone). Exits non-zero when the install or any test fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tags=${1:-/o3p_mcp}
manifest="$here/../o3p_mcp/__manifest__.py"
series=${ODOO_SERIES:-$(sed -nE 's/.*"version": "([0-9]+\.0)\..*/\1/p' "$manifest")}
if [ -z "$series" ]; then
    echo "Cannot read the Odoo series from $manifest; set ODOO_SERIES." >&2
    exit 2
fi
export ODOO_SERIES=$series
project=o3p-mcp-test
[ "$series" = "20.0" ] || project="o3p-mcp-test${series%%.*}"
project=${O3P_MCP_TEST_PROJECT:-$project}
extra=${O3P_MCP_TEST_MODULES-calendar,project}
compose=(docker compose -p "$project" -f "$here/compose.test.yaml")
echo "odoo:$series, compose project $project"
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
