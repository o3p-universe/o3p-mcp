"""Regenerate the data API and SQL parts of tests/fixtures/contract_20.json.

Run with the 20.0 branch's code, in `odoo shell` on a database where o3p_mcp
is installed and the SQL sandbox of scripts/compose.test.yaml is reachable
(host "sandbox", O3P_SQL_PASSWORD set); the JSON object is printed on
stdout. scripts/contract-20.sh does all of it. The other keys of the
fixture (introspection answers) are kept as they are.
"""

import json
import sys

from odoo.sql_db import db_connect
from odoo.tools import json_default

from odoo.addons.o3p_mcp.models import api as api_module
from odoo.addons.o3p_mcp.models import api_sql
from odoo.addons.o3p_mcp.models.api import ERRORS, ApiError

# The cases the Odoo 18 parity test replays (it reads them from the fixture).
SQL_CASES = {
    "success": {
        "query": "SELECT 1 AS one, 'é'::text AS t, 1.5::numeric AS n, DATE '2026-11-14' AS d,"
        " TIMESTAMP '2026-11-14 10:20:30' AS ts, INTERVAL '90 minutes' AS i,"
        " '{\"a\": [1, 2]}'::jsonb AS j, '\\xdead'::bytea AS b, ARRAY[1, 2] AS a, NULL::int AS z",
        "limit": 5,
    },
    "truncated": {"query": "SELECT g FROM generate_series(1, 3) AS g", "limit": 2},
    "gate_refused": {"query": "SELECT 1; SELECT 2"},
    "syntax_error": {"query": "SELECT 1 FROM WHERE"},
    "column_error": {"query": "SELECT nope"},
    "missing_table": {"query": "SELECT * FROM no_such_table"},
    "invalid_params": {"query": "SELECT 1", "limit": 0},
}
SETTING_CASES = {
    "row_cap": ("o3p_mcp.sql_max_rows", "1", {"query": "SELECT count(*) FROM res_partner"}),
    "timeout": ("o3p_mcp.api_timeout_seconds", "1", {"query": "SELECT pg_sleep(10)"}),
    "unavailable": ("o3p_mcp.sql_host", "", {"query": "SELECT 1"}),
}


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False, default=json_default)


def answer(api, params):
    try:
        result = api._run("sql", params)[0]
    except ApiError as error:
        return {"status": error.status, "body": dumps(error.to_dict())}
    result["elapsedMs"] = 0
    return {"status": 200, "body": dumps({"result": result})}


params = env["ir.config_parameter"].sudo()
params.set_str("o3p_mcp.sql_host", "sandbox")
user = env["res.users"].with_context(no_reset_password=True).create(
    {
        "name": "Contract User",
        "login": "o3p_mcp_contract",
        "lang": "en_US",
        "tz": "UTC",
        "group_ids": [(6, 0, (env.ref("base.group_user") | env.ref("o3p_mcp.group_mcp_user")).ids)],
    }
)
api = env["o3p.mcp.api"].with_user(user).with_context(lang="en_US", tz="UTC")
sql = {name: {"params": case, "setting": None, **answer(api, case)} for name, case in SQL_CASES.items()}
for name, (key, value, case) in SETTING_CASES.items():
    previous = params.get_str(key)
    params.set_str(key, value)
    sql[name] = {"params": case, "setting": [key, value], **answer(api, case)}
    params.set_str(key, previous or "")
params.set_str("o3p_mcp.sql_host", "sandbox")
holder = db_connect(env.cr.dbname).cursor()
base = (user.id % 2**24) * api_sql.MAX_USER_SLOTS
for slot in range(api_sql.DEFAULT_SQL_USER_CONCURRENCY):
    holder.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [api_sql.LOCK_USER_SLOT, base + slot])
# The user's slots are taken by another connection, as by running queries.
sql["too_many_requests"] = {"params": {"query": "SELECT 1"}, "setting": None, **answer(api, {"query": "SELECT 1"})}
holder.rollback()
holder.close()
env.cr.rollback()

json.dump(
    {
        "data_api_errors": [
            {"code": code, "status": status, "body": dumps(ApiError(code).to_dict())}
            for code, (status, _message) in ERRORS.items()
        ],
        "data_api_messages": {
            name: getattr(api_module, name) for name in sorted(dir(api_module)) if name.startswith("MSG_")
        },
        "sql_messages": {
            name: getattr(api_sql, name) for name in sorted(dir(api_sql)) if name.startswith("MSG_")
        },
        "sql_answers": sql,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=2,
)
print()
