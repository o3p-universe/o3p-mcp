import json
import time
from unittest.mock import patch

import psycopg2

from odoo.sql_db import db_connect
from odoo.tests import tagged

from ..models import api_sql
from ..models.oauth import set_config
from ..models.api_sql import LOCK_SANDBOX_ROLE, LOCK_USER_SLOT, MAX_USER_SLOTS
from .common import RESOURCE
from .sql_common import FIELD_MARKER, HIDDEN_MARKER, O3pMcpSqlCase


@tagged("post_install", "-at_install")
class TestSqlContract(O3pMcpSqlCase):
    def test_result_shape_and_types(self):
        result = self.sql_result(
            "SELECT id, name, color, 1.5::numeric AS ratio, 12345678901::bigint AS big,"
            " DATE '2026-11-14' AS day, TIMESTAMP '2026-11-14 10:20:30' AS moment,"
            " INTERVAL '90 minutes' AS span, '{\"a\": [1, 2]}'::jsonb AS doc,"
            " '\\xdeadbeef'::bytea AS raw, ARRAY[1, 2] AS list, NULL::text AS nothing"
            " FROM res_partner WHERE name LIKE 'MCPT %' ORDER BY color"
        )
        self.assertEqual(set(result), {"columns", "rows", "truncated", "elapsedMs", "tables"})
        self.assertEqual(
            result["columns"],
            [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "character varying"},
                {"name": "color", "type": "integer"},
                {"name": "ratio", "type": "numeric"},
                {"name": "big", "type": "bigint"},
                {"name": "day", "type": "date"},
                {"name": "moment", "type": "timestamp without time zone"},
                {"name": "span", "type": "interval"},
                {"name": "doc", "type": "jsonb"},
                {"name": "raw", "type": "bytea"},
                {"name": "list", "type": "integer[]"},
                {"name": "nothing", "type": "text"},
            ],
        )
        self.assertEqual(
            result["rows"][0],
            [
                self.alpha.id, "MCPT Alpha", 3, 1.5, 12345678901, "2026-11-14",
                "2026-11-14T10:20:30", 5400.0, {"a": [1, 2]}, "\\xdeadbeef", [1, 2], None,
            ],
        )
        self.assertEqual([row[1] for row in result["rows"]], ["MCPT Alpha", "MCPT Beta"])
        self.assertFalse(result["truncated"])
        self.assertIsInstance(result["elapsedMs"], int)
        self.assertEqual(result["tables"], [{"name": "res_partner", "model": "res.partner", "rows": 2 + self.base_visible()}])

    def base_visible(self):
        """Partners the restricted user may read besides Alpha and Beta."""
        return self.env["res.partner"].with_user(self.restricted).with_context(active_test=False).search_count(
            [("name", "not like", "MCPT %")]
        )

    def test_limit_and_truncation(self):
        query = "SELECT g FROM generate_series(1, 5) AS g"
        result = self.sql_result(query, limit=2)
        self.assertEqual((result["rows"], result["truncated"]), ([[1], [2]], True))
        self.assertEqual(self.sql_result(query)["rows"], [[g] for g in range(1, 6)])
        self.assertEqual(self.sql_result(query)["tables"], [])
        for params in (
            {"query": query, "limit": 0},
            {"query": query, "limit": 1001},
            {"query": query, "limit": "5"},
            {"query": ""},
            {"query": 42},
            {"query": "SELECT 1", "model": "res.partner"},
            {"query": "SELECT " + "1" * 20_001},
        ):
            with self.subTest(params=str(params)[:60]):
                raw = json.dumps({"token": self.sql_tokens["restricted"], "params": params})
                self.assertApiError(self.call("sql", raw=raw), "invalid_params")

    def test_query_errors_carry_postgresql_details(self):
        error = self.sql_error("SELECT nope FROM res_partner", sqlstate="42703")
        self.assertEqual(error["details"]["position"], 8)
        self.assertIn("nope", error["details"]["message"])
        error = self.sql_error("SELECT 1 FROM WHERE", sqlstate="42601")
        self.assertEqual(error["details"]["position"], 15)
        self.sql_error("SELECT * FROM no_such_table", sqlstate="42P01")
        error = self.sql_error("SELECT ('a' || repeat('b', 900))::int", sqlstate="22P02")
        self.assertEqual(len(error["details"]["message"]), 500)
        for query in (
            "SELECT 1; SELECT 2",
            "DELETE FROM res_partner",
            "SELECT * INTO TEMP copy FROM res_partner",
            "SELECT id FROM res_partner FOR UPDATE",
            "COPY res_partner TO STDOUT",
        ):
            with self.subTest(query=query):
                self.sql_error(query, sqlstate="0A000")

    def test_scope_and_configuration(self):
        _token, response = self.env["o3p.mcp.token"]._issue(
            self.client, self.restricted, {"okf.read", "odoo.read"}, RESOURCE
        )
        raw = json.dumps({"token": response["access_token"], "params": {"query": "SELECT 1"}})
        self.assertApiError(self.call("sql", raw=raw), "insufficient_scope")
        set_config(self.env, "o3p_mcp.sql_host", "")
        self.assertApiError(self.sql_call("SELECT 1"), "unavailable")
        set_config(self.env, "o3p_mcp.sql_host", self.sandbox_host)
        with patch.object(api_sql, "sql_password", return_value=""):
            self.assertApiError(self.sql_call("SELECT 1"), "unavailable")
        with patch.object(api_sql, "sql_password", return_value="wrong"):
            self.assertApiError(self.sql_call("SELECT 1"), "unavailable")
        self.assertEqual(self.sql_result("SELECT 1")["rows"], [[1]])

    def test_audit_records_tables_never_the_query(self):
        self.sql_result("SELECT name FROM res_partner WHERE name = 'sentinel-sql-text'")
        self.sql_error("SELECT 'sentinel-sql-error' FROM res_partner, no_such_table")
        logs = self.env["o3p.mcp.api.log"].search([("op", "=", "sql")], order="id")
        self.assertEqual(
            [(log.status, log.error or False, log.tables, log.row_count) for log in logs],
            [(200, False, "res_partner", 0), (422, "query_error", "res_partner", 0)],
        )
        self.assertEqual(logs[0].user_id, self.restricted)
        self.env.flush_all()
        self.env.cr.execute("SELECT row_to_json(l)::text FROM o3p_mcp_api_log l")
        dump = "\n".join(row for (row,) in self.env.cr.fetchall())
        self.assertNotIn("sentinel", dump)


@tagged("post_install", "-at_install")
class TestSqlLimits(O3pMcpSqlCase):
    def test_row_cap_per_table(self):
        set_config(self.env, "o3p_mcp.sql_max_rows", 2)
        self.assertApiError(self.sql_call("SELECT count(*) FROM res_partner", user="admin"), "result_too_large")
        companies = self.env["res.company"].with_user(self.admin).search_count([])
        self.assertLessEqual(companies, 2)
        self.assertEqual(self.sql_result("SELECT count(*) FROM res_company", user="admin")["rows"], [[companies]])

    def test_byte_cap_per_query(self):
        set_config(self.env, "o3p_mcp.sql_max_copy_bytes", 200)
        self.assertApiError(
            self.sql_call("SELECT name, email, street FROM res_partner", user="admin"), "result_too_large"
        )
        self.assertEqual(self.sql_result("SELECT 1")["rows"], [[1]])

    def test_answer_size_cap(self):
        set_config(self.env, "o3p_mcp.api_max_response_bytes", 300)
        self.assertApiError(
            self.sql_call("SELECT repeat('x', 1000)", user="admin"), "result_too_large"
        )

    def test_timeouts(self):
        set_config(self.env, "o3p_mcp.api_timeout_seconds", 2)
        for query in (
            "SELECT pg_sleep(30)",
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
            "SELECT count(*) FROM generate_series(1, 100000) a, generate_series(1, 100000) b",
            "SELECT set_config('statement_timeout', '0', false), pg_sleep(30)",
        ):
            with self.subTest(query=query[:40]):
                started = time.monotonic()
                self.assertApiError(self.sql_call(query), "timeout")
                self.assertLess(time.monotonic() - started, 6)

    def test_temporary_file_limit(self):
        # The sandbox roles cannot spill more than temp_file_limit to disk.
        error = self.sql_error("SELECT count(*) FROM generate_series(1, 1000000000)", sqlstate="53400")
        self.assertIn("temp_file_limit", error["details"]["message"])

    def test_watchdog_cancels_what_statement_timeout_misses(self):
        # A session whose statement_timeout no longer applies.
        set_config(self.env, "o3p_mcp.api_timeout_seconds", 2)

        def no_limit(api_model, cursor, milliseconds):
            cursor.execute("SELECT set_config('statement_timeout', '0', true)")

        started = time.monotonic()
        with patch.object(api_sql.O3pMcpApiSql, "_sql_limit_query", no_limit):
            self.assertApiError(self.sql_call("SELECT pg_sleep(30)"), "timeout")
        self.assertLess(time.monotonic() - started, 7)

    def hold(self, namespace, keys):
        """Hold advisory locks from another connection, as a running query would."""
        cursor = db_connect(self.env.cr.dbname).cursor()
        self.addCleanup(cursor.close)
        self.addCleanup(cursor.rollback)
        for key in keys:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [namespace, key])
            self.assertTrue(cursor.fetchone()[0])
        return cursor

    def test_concurrency_per_user(self):
        set_config(self.env, "o3p_mcp.sql_user_concurrency", 2)
        base = self.restricted.id * MAX_USER_SLOTS
        holder = self.hold(LOCK_USER_SLOT, [base, base + 1])
        self.assertApiError(self.sql_call("SELECT 1"), "too_many_requests")
        # Other users are not affected.
        self.assertEqual(self.sql_result("SELECT 1", user="restricted2")["rows"], [[1]])
        holder.rollback()
        self.assertEqual(self.sql_result("SELECT 1")["rows"], [[1]])

    def test_sandbox_roles_are_used_one_query_at_a_time(self):
        set_config(self.env, "o3p_mcp.sql_roles", 4)
        holder = self.hold(LOCK_SANDBOX_ROLE, [1, 2, 3])
        result = self.sql_result("SELECT current_user")
        self.assertEqual(result["rows"], [["o3p_sqlbox_4"]])
        self.hold(LOCK_SANDBOX_ROLE, [4])
        self.assertApiError(self.sql_call("SELECT 1"), "too_many_requests")
        holder.rollback()
        self.assertEqual(self.sql_result("SELECT current_user")["rows"], [["o3p_sqlbox_1"]])


@tagged("post_install", "-at_install")
class TestSqlSandboxSetup(O3pMcpSqlCase):
    def test_roles_cannot_see_or_signal_each_other(self):
        settings = self.env["o3p.mcp.api"]._sql_settings()
        Api = self.env["o3p.mcp.api"]
        first = Api._sql_connect(settings, 1, 10_000)
        second = Api._sql_connect(settings, 2, 10_000)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        second.autocommit = True
        second.cursor().execute("SELECT 'other user query text'")
        cursor = first.cursor()
        cursor.execute("SELECT query FROM pg_stat_activity WHERE pid = %s", [second.get_backend_pid()])
        self.assertEqual(cursor.fetchall(), [("<insufficient privilege>",)])
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            cursor.execute("SELECT pg_terminate_backend(%s)", [second.get_backend_pid()])
        first.rollback()
        # One connection per role.
        with self.assertRaises(psycopg2.OperationalError):
            Api._sql_connect(settings, 1, 10_000)


@tagged("post_install", "-at_install")
class TestSqlVisibility(O3pMcpSqlCase):
    def test_restricted_users_see_their_rows_only(self):
        names = lambda user: {row[0] for row in self.sql_result("SELECT name FROM res_partner WHERE name LIKE 'MCPT %'", user=user)["rows"]}  # noqa: E731
        self.assertEqual(names("restricted"), {"MCPT Alpha", "MCPT Beta"})
        # Color 100 is below restricted2's limit of 150; the partner of a
        # company belongs to no company.
        self.assertEqual(
            names("restricted2"),
            {"MCPT Alpha", "MCPT Beta", "MCPT Archived", "MCPT Hidden One", "MCPT Company B"},
        )
        self.assertEqual(
            names("multi"),
            {"MCPT Alpha", "MCPT Beta", "MCPT Archived", "MCPT Hidden One", "MCPT Hidden Two",
             "MCPT Company B", "MCPT Company B Partner", "MCPT Company B Archived"},
        )
        self.assertNotIn("MCPT Company B Partner", names("admin"))
        # Totals cover the visible rows only.
        total = self.sql_result("SELECT sum(color) FROM res_partner WHERE name LIKE 'MCPT %'")["rows"]
        self.assertEqual(total, [[7]])

    def test_hidden_fields_and_denylist(self):
        with self.protected_field("res.partner", "comment"):
            error = self.sql_error("SELECT comment FROM res_partner", sqlstate="42703")
            self.assertNotIn(FIELD_MARKER, json.dumps(error))
            rows = self.sql_result("SELECT * FROM res_partner WHERE name LIKE 'MCPT %'")
            self.assertNotIn("comment", [column["name"] for column in rows["columns"]])
            self.assertNotIn(FIELD_MARKER, json.dumps(rows))
        self.assertIn(FIELD_MARKER, json.dumps(self.sql_result("SELECT comment FROM res_partner")))
        for user in ("restricted", "admin"):
            with self.subTest(user=user):
                self.sql_error("SELECT password FROM res_users", user=user, sqlstate="42703")
                self.sql_error("SELECT * FROM ir_config_parameter", user=user, sqlstate="42P01")
                self.sql_error("SELECT * FROM o3p_mcp_token", user=user, sqlstate="42P01")
                self.sql_error("SELECT * FROM ir_attachment", user=user, sqlstate="42P01")
                columns = self.sql_result("SELECT * FROM res_users", user=user)["columns"]
                self.assertFalse([c for c in columns if "password" in c["name"] or "token" in c["name"]])
        set_config(self.env, "o3p_mcp.api_denied_models", "res.partner*")
        self.sql_error("SELECT name FROM res_partner", user="admin", sqlstate="42P01")
        self.sql_error("SELECT * FROM res_partner_res_partner_category_rel", user="admin", sqlstate="42P01")
        # res.users is stored partly in res.partner (_inherits): hidden with it.
        self.sql_error("SELECT login FROM res_users", user="admin", sqlstate="42P01")

    def test_many2many_needs_both_sides(self):
        query = (
            "SELECT p.name, c.name FROM res_partner_res_partner_category_rel r"
            " JOIN res_partner p ON p.id = r.partner_id JOIN res_partner_category c ON c.id = r.category_id"
            " WHERE p.name LIKE 'MCPT %' ORDER BY 1, 2"
        )
        self.assertEqual(
            sorted(self.sql_result(query)["rows"]),
            [["MCPT Alpha", "MCP visible"], ["MCPT Alpha", "MCPT Secret Category"], ["MCPT Beta", "MCP visible"]],
        )
        relation = "SELECT partner_id, category_id FROM res_partner_res_partner_category_rel"
        pairs = {tuple(row) for row in self.sql_result(relation, user="restricted2")["rows"]}
        self.assertNotIn((self.alpha.id, self.secret_category.id), pairs)
        self.assertIn((self.alpha.id, self.visible.id), pairs)
        pairs = {tuple(row) for row in self.sql_result(relation)["rows"]}
        self.assertFalse({self.hidden.id, self.hidden2.id} & {partner for partner, _c in pairs})

    def test_translated_and_company_dependent_values(self):
        # Translated fields come in the user's language, as plain text.
        self.env["res.lang"]._activate_lang("fr_FR")
        category = self.env["res.partner.category"].create({"name": "MCPT Colour"})
        category.with_context(lang="fr_FR").name = "MCPT Couleur"
        self.restricted.lang = "fr_FR"
        query = "SELECT name, pg_typeof(name)::text FROM res_partner_category WHERE name LIKE 'MCPT C%'"
        self.assertEqual(self.sql_result(query)["rows"], [["MCPT Couleur", "text"]])
        self.assertEqual(self.sql_result(query, user="admin")["rows"], [["MCPT Colour", "text"]])
        # Company-dependent fields come as the value for the user's company.
        dependent = [
            (model, field)
            for model in self.env.registry.values()
            for field in model._fields.values()
            if field.company_dependent and field.store and field.column_type and not model._abstract
        ]
        for model, field in dependent[:3]:
            with self.subTest(field=str(field)):
                result = self.direct(self.admin, f'SELECT pg_typeof("{field.name}")::text FROM "{model._table}" LIMIT 1')
                if isinstance(result, dict) and result["rows"]:
                    self.assertNotEqual(result["rows"][0][0], "jsonb")

    def test_rows_are_what_the_orm_returns(self):
        query = "SELECT id, active, company_id FROM res_partner ORDER BY id"
        for name, user in self.users.items():
            with self.subTest(user=name):
                records = self.env["res.partner"].with_user(user).with_context(active_test=False).search([], order="id")
                expected = [[r.id, r.active, r.company_id.id or None] for r in records.sudo()]
                self.assertEqual(self.sql_result(query, user=name)["rows"], expected)
        self.assertNotIn(HIDDEN_MARKER, json.dumps(self.sql_result("SELECT name FROM res_partner")))
