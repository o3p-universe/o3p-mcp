#!/usr/bin/env bash
# Regenerate the 20.0 parts of o3p_mcp/tests/fixtures/contract_20.json.
#
# Usage: scripts/contract-20.sh <path of a checkout of the 20.0 branch>
#
# Installs o3p_mcp from that checkout on odoo:20.0 (with the SQL sandbox of
# its compose file), runs scripts/contract_20_sql.py there and merges the
# answers into this branch's fixture, keeping its other keys.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
checkout=$(cd "${1:?path of a 20.0 checkout}" && pwd)
fixture="$here/../o3p_mcp/tests/fixtures/contract_20.json"
compose=(docker compose -p "${O3P_MCP_TEST_PROJECT:-o3p-mcp-contract}" -f "$checkout/scripts/compose.test.yaml")
out=$(mktemp)
cleanup() { "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$out"; }
trap cleanup EXIT
"${compose[@]}" build --quiet odoo
"${compose[@]}" up -d --wait db sandbox >/dev/null
"${compose[@]}" run --rm --no-deps -T odoo odoo --addons-path=/mnt/extra-addons \
    -d contract -i o3p_mcp --stop-after-init --log-level=warn >/dev/null
"${compose[@]}" run --rm --no-deps -T odoo odoo shell --addons-path=/mnt/extra-addons \
    -d contract --log-level=error < "$here/contract_20_sql.py" | sed -n '/^{/,/^}/p' > "$out"
revision=$(git -C "$checkout" rev-parse --short HEAD)
python3 - "$fixture" "$out" "$revision" <<'PY'
import json, sys
fixture, generated, revision = sys.argv[1:]
data = json.load(open(fixture))
data.update(json.load(open(generated)))
data["source"] = (
    f"o3p_mcp branch 20.0 at {revision} on Odoo 20.0 (generated from the code, not edited by hand;"
    " data API and SQL parts by scripts/contract-20.sh)"
)
with open(fixture, "w") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
echo "updated $fixture from 20.0 at $revision"
