#!/usr/bin/env bash
# Checks an upgrade of o3p_mcp on a throw-away database: installs the addon as
# of a git ref, runs a "before" script in `odoo shell`, upgrades to the working
# tree, runs an "after" script and succeeds when that prints "UPGRADE OK".
#
# Usage: scripts/check-upgrade.sh <git ref> <before.py> <after.py>
#   scripts/check-upgrade.sh origin/18.0 scripts/upgrade/sql-group-before.py scripts/upgrade/sql-group-after.py
#
# Uses the stack of scripts/run-tests.sh under the compose project
# "o3p-mcp-upgrade" (or $O3P_MCP_UPGRADE_PROJECT) and removes it afterwards.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname "$here")
ref=${1:?git ref of the version to upgrade from}
before=${2:?script to run before the upgrade}
after=${3:?script to run after the upgrade}
# The Odoo series of the stack, read from the manifest as scripts/run-tests.sh does.
export ODOO_SERIES=${ODOO_SERIES:-$(sed -nE 's/.*"version": "([0-9]+\.0)\..*/\1/p' "$repo/o3p_mcp/__manifest__.py")}
compose=(docker compose -p "${O3P_MCP_UPGRADE_PROJECT:-o3p-mcp-upgrade}" -f "$here/compose.test.yaml")
old=$(mktemp -d)

cleanup() {
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$old"
}
trap cleanup EXIT

git -C "$repo" archive "$ref" o3p_mcp | tar -x -C "$old"
chmod -R a+rX "$old"
from=$(sed -n 's/.*"version": "\(.*\)".*/\1/p' "$old/o3p_mcp/__manifest__.py")
to=$(sed -n 's/.*"version": "\(.*\)".*/\1/p' "$repo/o3p_mcp/__manifest__.py")
echo "upgrade $from ($ref) -> $to (working tree)"

"${compose[@]}" build --quiet odoo
"${compose[@]}" up -d --wait db sandbox >/dev/null
# odoo <addons path> <server|shell> [options]: the old tree for the install,
# this repository after.
odoo() {
    local addons=$1 command=$2
    shift 2
    "${compose[@]}" run --rm --no-deps -T -v "$old:/mnt/old-addons:ro" odoo odoo "$command" \
        --addons-path="$addons" -d o3p_mcp_upgrade --log-level=warn "$@"
}

odoo /mnt/old-addons server -i o3p_mcp --stop-after-init
odoo /mnt/old-addons shell --no-http < "$before"
odoo /mnt/extra-addons server -u o3p_mcp --stop-after-init
result=$(odoo /mnt/extra-addons shell --no-http < "$after")
printf '%s\n' "$result"
if grep -qx "UPGRADE OK" <<< "$result"; then
    echo "OK"
else
    echo "FAILED"
    exit 1
fi
