"""The answers on Odoo 18 are the same bytes as on the 20.0 branch.

``fixtures/contract_20.json`` was generated from the 20.0 branch code; the
introspection answer is also checked against ``introspection_active.json``.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

from psycopg2 import errors as pg_errors

from odoo.sql_db import db_connect
from odoo.tests import tagged
from odoo.tools import json_default

from ..models import api as api_module
from ..models import api_sql
from ..models.api import ERRORS, ApiError
from ..models.oauth import get_config, set_config
from .api_common import O3pMcpApiCase, basic
from .common import RESOURCE
from .sql_common import O3pMcpSqlCase


FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "contract_20.json").read_text())
INTROSPECTION = json.loads((FIXTURES / "introspection_active.json").read_text())
INTROSPECT_URL = "/o3p_mcp/oauth/introspect"


@tagged("post_install", "-at_install")
class TestContractParity(O3pMcpApiCase):
    def expected(self, code):
        return next(item for item in CONTRACT["data_api_errors"] if item["code"] == code)

    def test_error_details_match_20(self):
        self.assertEqual(api_module.REASONS, CONTRACT["data_api_reasons"])
        self.assertEqual(api_module.MODEL_HINTS, CONTRACT["data_api_model_hints"])
        example = api_module.invalid(
            "granularity_required", "create_date", message=api_module.MSG_GROUPBY
        ).to_dict()
        self.assertEqual(
            json.dumps(example, ensure_ascii=False, default=json_default),
            CONTRACT["data_api_details_example"],
        )
        response = self.call(
            "group", {"model": "res.partner", "groupby": ["create_date"], "aggregates": ["__count"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.text, CONTRACT["data_api_details_example"])

    def test_error_table_matches_20(self):
        self.assertEqual(list(ERRORS), [item["code"] for item in CONTRACT["data_api_errors"]])
        for item in CONTRACT["data_api_errors"]:
            with self.subTest(code=item["code"]):
                error = ApiError(item["code"])
                self.assertEqual(error.status, item["status"])
                self.assertEqual(
                    json.dumps(error.to_dict(), ensure_ascii=False, default=json_default),
                    item["body"],
                )
        messages = {
            name: getattr(api_module, name)
            for name in sorted(dir(api_module))
            if name.startswith("MSG_")
        }
        self.assertEqual(messages, CONTRACT["data_api_messages"])

    def test_live_error_bodies_match_20(self):
        _token, scoped = self.env["o3p.mcp.token"]._issue(
            self.client, self.restricted, {"okf.read"}, RESOURCE
        )
        api_class = type(self.env["o3p.mcp.api"])
        cases = {
            "invalid_params": lambda: self.call("models", raw="not json"),
            "invalid_client": lambda: self.call("models", headers={}),
            "invalid_token": lambda: self.call("models", token="not-a-token"),
            "insufficient_scope": lambda: self.call("models", token=scoped["access_token"]),
            "access_denied": lambda: self.call("describe", {"model": "ir.cron"}),
            "unknown_op": lambda: self.call("nope", {"query": "select 1"}),
            "unknown_model": lambda: self.call("describe", {"model": "no.such.model"}),
        }
        for code, send in cases.items():
            with self.subTest(code=code):
                response = send()
                self.assertEqual(response.status_code, self.expected(code)["status"])
                body = response.json()
                details = body["error"].pop("details", None)
                self.assertEqual(
                    json.dumps(body, ensure_ascii=False, default=json_default),
                    self.expected(code)["body"],
                )
                if code == "access_denied":
                    # Validation errors add details (fixed texts compared above).
                    self.assertEqual(details, {
                        "model": "ir.cron",
                        "reason": "no_model_access",
                        "hint": CONTRACT["data_api_reasons"]["no_model_access"],
                    })
                else:
                    self.assertIsNone(details)
        with patch.object(api_class, "_op_models", side_effect=ValueError("detail")):
            response = self.call("models")
        self.assertEqual(response.text, self.expected("query_error")["body"])
        with patch.object(api_class, "_op_models", side_effect=pg_errors.QueryCanceled()):
            response = self.call("models")
        self.assertEqual(response.text, self.expected("timeout")["body"])
        set_config(self.env, "o3p_mcp.api_max_response_bytes", 100)
        response = self.call("models", user="admin")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.text, self.expected("result_too_large")["body"])

    def test_introspection_matches_20(self):
        headers = basic(self.server.client_id, self.server_secret)
        token = self.tokens["restricted"]
        response = self.url_open(INTROSPECT_URL, data={"token": token}, headers=headers)
        answer = response.json()
        self.assertEqual(list(answer), list(INTROSPECTION))
        for key, value in INTROSPECTION.items():
            self.assertIs(type(answer[key]), type(value), key)
        self.assertGreater(answer["exp"], time.time())
        self.assertEqual(
            response.text, json.dumps(answer, ensure_ascii=False, default=json_default)
        )
        inactive = self.url_open(INTROSPECT_URL, data={"token": "x"}, headers=headers)
        self.assertEqual(inactive.text, CONTRACT["introspection_inactive"])
        refused = self.url_open(INTROSPECT_URL, data={"token": token}, headers={})
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.text, CONTRACT["oauth_invalid_client"])


@tagged("post_install", "-at_install")
class TestSqlContractParity(O3pMcpSqlCase):
    """The sql operation answers on Odoo 18 with the 20.0 branch's bytes.

    The cases and the answers come from the fixture (scripts/contract-20.sh
    replays them on the 20.0 code); only ``elapsedMs`` may differ.
    """

    def test_sql_messages_match_20(self):
        messages = {name: getattr(api_sql, name) for name in sorted(dir(api_sql)) if name.startswith("MSG_")}
        self.assertEqual(messages, CONTRACT["sql_messages"])

    def test_sql_answers_match_20(self):
        for name, case in CONTRACT["sql_answers"].items():
            with self.subTest(case=name):
                holder = None
                if case["setting"]:
                    key, value = case["setting"]
                    previous = get_config(self.env, key)
                    set_config(self.env, key, value)
                if name == "too_many_requests":
                    holder = db_connect(self.env.cr.dbname).cursor()
                    base = (self.restricted.id % 2**24) * api_sql.MAX_USER_SLOTS
                    for slot in range(api_sql.DEFAULT_SQL_USER_CONCURRENCY):
                        holder.execute(
                            "SELECT pg_try_advisory_xact_lock(%s, %s)", [api_sql.LOCK_USER_SLOT, base + slot]
                        )
                try:
                    raw = json.dumps({"token": self.sql_tokens["restricted"], "params": case["params"]})
                    response = self.call("sql", raw=raw)
                finally:
                    if holder is not None:
                        holder.rollback()
                        holder.close()
                    if case["setting"]:
                        set_config(self.env, key, previous)
                self.assertEqual(response.status_code, case["status"])
                text = response.text
                if response.status_code == 200:
                    body = response.json()
                    body["result"]["elapsedMs"] = 0
                    text = json.dumps(body, ensure_ascii=False, default=json_default)
                self.assertEqual(text, case["body"])
