import base64
import json
from datetime import timedelta
from urllib.parse import quote_plus
from unittest.mock import patch

from psycopg2 import errors as pg_errors

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from ..models.api import DEFAULT_DENIED_FIELDS
from ..models.oauth import hash_secret, set_config
from .api_common import API_URL, O3pMcpApiCase, basic
from .common import REDIRECT_URI, RESOURCE, url_request


@tagged("post_install", "-at_install")
class TestApiAuthentication(O3pMcpApiCase):
    def test_client_must_be_the_resource_server(self):
        public = basic(self.client.client_id, "")
        confidential = self.env["o3p.mcp.client"].create(
            {
                "name": "Confidential",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        secret = confidential._set_new_secret()
        for headers in (
            {},
            basic(self.server.client_id, "wrong"),
            basic("c_unknown", self.server_secret),
            public,
            basic(confidential.client_id, secret),
            {"Authorization": f"Bearer {self.tokens['restricted']}"},
        ):
            with self.subTest(headers=list(headers)):
                response = self.call("models", headers=headers)
                self.assertApiError(response, "invalid_client")
                self.assertEqual(response.headers["WWW-Authenticate"], 'Basic realm="o3p_mcp"')

    def test_basic_credentials_are_form_urlencoded(self):
        # RFC 6749 section 2.3.1: the client form-urlencodes id and secret.
        client_id, secret = "c_ odd+id:é", "s p+a:ce é"
        self.server.client_id = client_id
        self.server.sudo().secret_hash = hash_secret(secret)
        raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
        headers = {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
        self.assertEqual(self.call("models", headers=headers).status_code, 200)
        # Without the encoding the plus sign reads as a space.
        raw = f"{client_id.replace(':', '%3A')}:{secret}".encode()
        headers = {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
        self.assertApiError(self.call("models", headers=headers), "invalid_client")

    def test_body_must_be_an_object(self):
        for raw in ("not json", "[]", '"token"'):
            with self.subTest(raw=raw):
                self.assertApiError(self.call("models", raw=raw), "invalid_params")
        raw = json.dumps({"token": self.tokens["restricted"], "params": ["x"]})
        self.assertApiError(self.call("models", raw=raw), "invalid_params")

    def test_token_checks(self):
        Token = self.env["o3p.mcp.token"]
        cases = {
            "missing": None,
            "unknown": "not-a-token",
            "refresh": Token._issue(self.client, self.restricted, {"okf.read", "odoo.read"}, RESOURCE)[1]["refresh_token"],
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                raw = json.dumps({"token": value, "params": {}})
                self.assertApiError(self.call("models", raw=raw), "invalid_token")
        # Portal users hold no scope, whatever their groups.
        self.assertApiError(self.call("models", user="portal"), "invalid_token")

    def test_revoked_expired_and_inactive(self):
        Token = self.env["o3p.mcp.token"]

        def fresh():
            token, response = Token._issue(self.client, self.editor, {"okf.read", "odoo.read"}, RESOURCE)
            return token, response["access_token"]

        token, value = fresh()
        self.assertEqual(self.call("models", token=value).status_code, 200)
        token._revoke_family()
        self.assertApiError(self.call("models", token=value), "invalid_token")
        token, value = fresh()
        token.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertApiError(self.call("models", token=value), "invalid_token")
        token, value = fresh()
        self.client.active = False
        self.assertApiError(self.call("models", token=value), "invalid_token")
        self.client.active = True
        self.editor.active = False
        self.assertApiError(self.call("models", token=value), "invalid_token")

    def test_audience_must_be_the_configured_mcp_url(self):
        params = self.env["ir.config_parameter"].sudo()
        set_config(params.env, "o3p_mcp.resource_url", f"{RESOURCE}/")
        self.assertEqual(self.call("models").status_code, 200)
        set_config(params.env, "o3p_mcp.resource_url", "https://other.example.com/mcp")
        self.assertApiError(self.call("models"), "invalid_token")
        set_config(params.env, "o3p_mcp.resource_url", "")
        self.assertApiError(self.call("models"), "invalid_token")

    def test_scope_is_required(self):
        _token, response = self.env["o3p.mcp.token"]._issue(
            self.client, self.restricted, {"okf.read"}, RESOURCE
        )
        self.assertApiError(
            self.call("models", token=response["access_token"]), "insufficient_scope"
        )
        # The scope follows the user's groups at every call.
        self.remove_mcp_access(self.restricted)
        self.assertApiError(self.call("models"), "invalid_token")

    def test_unknown_operations(self):
        for op in ("write", "unlink", "nope", "SQL"):
            with self.subTest(op=op):
                self.assertApiError(self.call(op, {"query": "select 1"}), "unknown_op")
        # The tokens here lack odoo.sql.
        self.assertApiError(self.call("sql", {"query": "select 1"}), "insufficient_scope")
        response = self.url_open(API_URL + "models")
        self.assertEqual(response.status_code, 405)
        response = url_request(
            self,
            "OPTIONS",
            API_URL + "models",
            headers={
                "Origin": "https://inspector.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertNotIn("Access-Control-Allow-Origin", self.call("models").headers)


@tagged("post_install", "-at_install")
class TestApiModels(O3pMcpApiCase):
    def test_models_lists_readable_models_only(self):
        result = self.result("models", {"query": "partner"})["models"]
        names = [item["model"] for item in result]
        self.assertIn("res.partner", names)
        self.assertEqual(
            next(item for item in result if item["model"] == "res.partner")["name"], "Contact"
        )
        for item in result:
            self.assertEqual(set(item), {"model", "name"})
            self.assertTrue(self.env[item["model"]].with_user(self.restricted).has_access("read"))
        # A model only administrators may read.
        self.assertEqual(self.result("models", {"query": "ir.cron"})["models"], [])
        admin = [item["model"] for item in self.result("models", {"query": "ir.cron"}, user="admin")["models"]]
        self.assertIn("ir.cron", admin)

    def test_models_never_lists_hidden_transient_or_abstract_models(self):
        for query in ("ir.config_parameter", "o3p.mcp", "apikeys", "mail_server", "res.config"):
            with self.subTest(query=query):
                names = [item["model"] for item in self.result("models", {"query": query}, user="admin")["models"]]
                self.assertFalse(
                    [name for name in names if name.startswith(("ir.config_parameter", "o3p.mcp", "res.users.apikeys", "ir.mail_server", "res.config"))],
                    names,
                )
        names = [item["model"] for item in self.result("models", {"query": "base"}, user="admin")["models"]]
        self.assertNotIn("base", names)

    def test_models_pages(self):
        first = self.result("models", {"limit": 50}, user="admin")
        self.assertEqual(set(first), {"models", "total", "truncated"})
        self.assertGreater(first["total"], 50)
        self.assertTrue(first["truncated"])
        listed = [item["model"] for item in first["models"]]
        offset = 50
        while True:
            page = self.result("models", {"limit": 50, "offset": offset}, user="admin")
            self.assertEqual(page["total"], first["total"])
            listed += [item["model"] for item in page["models"]]
            offset += 50
            if not page["truncated"]:
                break
        self.assertEqual(len(listed), first["total"])
        self.assertEqual(len(set(listed)), len(listed))
        self.assertEqual(listed, sorted(listed))
        beyond = self.result("models", {"offset": first["total"]}, user="admin")
        self.assertEqual((beyond["models"], beyond["truncated"]), ([], False))
        query = self.result("models", {"query": "partner"}, user="admin")
        self.assertEqual(query["total"], len(query["models"]))
        self.assertFalse(query["truncated"])

    def test_models_parameters(self):
        self.assertEqual(len(self.result("models", {"limit": 2}, user="admin")["models"]), 2)
        for params in ({"limit": 51}, {"limit": 0}, {"limit": "5"}, {"query": 3}, {"query": "x" * 201},
                       {"offset": -1}, {"offset": 10_001}):
            with self.subTest(params=params):
                self.assertApiError(self.call("models", params), "invalid_params")


@tagged("post_install", "-at_install")
class TestApiDescribe(O3pMcpApiCase):
    def describe(self, model, user="restricted"):
        return self.result("describe", {"model": model}, user=user)

    def test_describe_shape(self):
        result = self.describe("res.partner")
        self.assertEqual(list(result), ["model", "name", "table", "fields"])
        self.assertEqual(result["model"], "res.partner")
        self.assertEqual(result["table"], "res_partner")
        self.assertEqual(self.describe("res.partner.category")["table"], "res_partner_category")
        fields_by_name = {item["name"]: item for item in result["fields"]}
        self.assertEqual(fields_by_name["color"]["type"], "integer")
        self.assertEqual(fields_by_name["category_id"]["relation"], "res.partner.category")
        self.assertIn("selection", fields_by_name["type"])
        for item in result["fields"]:
            self.assertLessEqual(
                {"name", "string", "type", "required", "readonly", "stored", "searchable", "sortable", "groupable"},
                set(item),
            )
            self.assertLessEqual(
                set(item),
                {"name", "string", "type", "relation", "required", "readonly", "stored", "searchable",
                 "sortable", "groupable", "selection", "help"},
            )
            self.assertNotEqual(item["type"], "binary")
        flags = {
            name: (fields_by_name[name]["stored"], fields_by_name[name]["searchable"],
                   fields_by_name[name]["sortable"], fields_by_name[name]["groupable"])
            for name in ("name", "color", "category_id", "child_ids", "parent_id", "display_name",
                         "active_lang_count")
        }
        self.assertEqual(flags, {
            "name": (True, True, True, True),
            "color": (True, True, True, True),
            "category_id": (False, True, False, True),
            "child_ids": (False, True, False, False),
            "parent_id": (True, True, True, True),
            "display_name": (False, True, False, False),
            "active_lang_count": (False, False, False, False),
        })

    def test_table_is_null_without_a_relation(self):
        # Models computed from a query have a _table name but no relation.
        with patch.object(type(self.env["res.partner.category"]), "_table", "o3p_mcp_no_such_table"):
            result = self.describe("res.partner.category")
        self.assertIsNone(result["table"])

    def test_field_groups_are_respected(self):
        with self.protected_field("res.partner", "function"):
            restricted = {item["name"] for item in self.describe("res.partner")["fields"]}
            admin = {item["name"] for item in self.describe("res.partner", user="admin")["fields"]}
        self.assertNotIn("function", restricted)
        self.assertIn("function", admin)

    def test_denylist_applies_to_administrators(self):
        names = {item["name"] for item in self.describe("res.users", user="admin")["fields"]}
        for hidden in ("password", "new_password", "api_key_ids", "image_1920", "avatar_128"):
            self.assertNotIn(hidden, names)
        self.assertIn("login", names)
        for model in ("ir.config_parameter", "o3p.mcp.token", "res.users.apikeys", "ir.mail_server"):
            with self.subTest(model=model):
                self.assertApiError(
                    self.call("describe", {"model": model}, user="admin"), "unknown_model"
                )

    def test_unknown_unreadable_transient(self):
        self.assertApiError(self.call("describe", {"model": "no.such.model"}), "unknown_model")
        self.assertApiError(self.call("describe", {"model": "res.config.settings"}, user="admin"), "unknown_model")
        self.assertApiError(self.call("describe", {"model": "base"}, user="admin"), "unknown_model")
        self.assertApiError(self.call("describe", {"model": 3}), "unknown_model")
        self.assertApiError(self.call("describe", {"model": "ir.cron"}), "access_denied")

    def test_denylist_setting(self):
        params = self.env["ir.config_parameter"].sudo()
        set_config(params.env, "o3p_mcp.api_denied_models", "res.partner*")
        set_config(params.env, "o3p_mcp.api_denied_fields", "color\n*phone*")
        self.assertApiError(self.call("describe", {"model": "res.partner"}), "unknown_model")
        names = {item["name"] for item in self.describe("res.users", user="admin")["fields"]}
        self.assertNotIn("phone", names)
        # Replacing the default list exposes what it used to hide.
        self.assertIn("password", names)
        set_config(params.env, "o3p_mcp.api_denied_models", "")
        self.assertApiError(
            self.call("describe", {"model": "ir.config_parameter"}, user="admin"), "unknown_model"
        )


@tagged("post_install", "-at_install")
class TestApiLimitsAndAudit(O3pMcpApiCase):
    def test_result_too_large(self):
        set_config(self.env, "o3p_mcp.api_max_response_bytes", 200)
        self.assertApiError(self.call("models", user="admin"), "result_too_large")

    def test_database_errors_are_mapped_without_detail(self):
        api_class = type(self.env["o3p.mcp.api"])
        with patch.object(api_class, "_op_models", side_effect=pg_errors.QueryCanceled()):
            self.assertApiError(self.call("models"), "timeout")
        with patch.object(api_class, "_op_models", side_effect=ValueError("secret detail")):
            response = self.call("models")
        self.assertApiError(response, "query_error")
        self.assertNotIn("secret detail", response.text)

    def test_operations_cannot_write(self):
        def writer(api_model, params):
            api_model.env["res.partner"].sudo().create({"name": "MCPT written"})
            api_model.env.flush_all()
            return [], None, 0

        with (
            patch.object(type(self.env["o3p.mcp.api"]), "_op_models", writer),
            mute_logger("odoo.sql_db"),
        ):
            self.assertApiError(self.call("models"), "query_error")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "MCPT written")]))
        log = self.env["o3p.mcp.api.log"].search([], limit=1)
        self.assertEqual((log.status, log.error), (422, "query_error"))

    def test_unexpected_errors_answer_query_error_and_are_recorded(self):
        api_class = type(self.env["o3p.mcp.api"])
        Log = self.env["o3p.mcp.api.log"]

        def broken_token_check(api_model, value, scope):
            api_model.env.cr.execute("SELECT o3p_mcp_no_such_column FROM res_partner")

        for label, patcher in (
            ("inside the operation", patch.object(api_class, "_op_models", side_effect=RuntimeError("boom-secret"))),
            ("before the operation", patch.object(api_class, "_check_token", side_effect=RuntimeError("boom-secret"))),
            ("database error before the operation", patch.object(api_class, "_check_token", broken_token_check)),
        ):
            with self.subTest(label), patcher, mute_logger("odoo.sql_db"):
                before = Log.search_count([])
                response = self.call("models")
                self.assertApiError(response, "query_error")
                self.assertEqual(response.json()["error"]["message"], "The query could not be run.")
                self.assertNotIn("boom", response.text)
                self.assertNotIn("no_such_column", response.text)
                log = Log.search([], limit=1)
                self.assertEqual(Log.search_count([]), before + 1)
                self.assertEqual((log.op, log.status, log.error), ("models", 422, "query_error"))

    def test_audit_rows_without_parameters(self):
        self.call("models", {"query": "sentinel-partner"})
        self.call("describe", {"model": "ir.cron"})
        self.call("models", headers={})
        logs = self.env["o3p.mcp.api.log"].search([], order="id")
        self.assertEqual(
            [(log.op, log.status, log.error or False) for log in logs],
            [("models", 200, False), ("describe", 403, "access_denied"), ("models", 401, "invalid_client")],
        )
        ok, denied, anonymous = logs
        self.assertEqual(ok.user_id, self.restricted)
        self.assertEqual(ok.client_id, self.client)
        self.assertEqual(denied.model, False)
        self.assertFalse(anonymous.user_id)
        self.env.flush_all()
        self.env.cr.execute("SELECT row_to_json(l)::text FROM o3p_mcp_api_log l")
        rows = "\n".join(row for (row,) in self.env.cr.fetchall())
        self.assertNotIn("sentinel", rows)
        self.assertNotIn(self.tokens["restricted"], rows)
        self.assertTrue(self.env["o3p.mcp.token"].search([("user_id", "=", self.restricted.id)], limit=1).last_used_at)

    def test_old_audit_rows_are_purged(self):
        Log = self.env["o3p.mcp.api.log"]
        old = Log.create({"op": "models", "status": 200})
        recent = Log.create({"op": "models", "status": 200})
        self.env.cr.execute(
            "UPDATE o3p_mcp_api_log SET create_date = create_date - interval '31 days' WHERE id = %s",
            [old.id],
        )
        self.env["o3p.mcp.token"]._purge_expired()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


MCPT = [["name", "=like", "MCPT %"]]


@tagged("post_install", "-at_install")
class TestApiSearchRead(O3pMcpApiCase):
    def search(self, user="restricted", **params):
        params = {"model": "res.partner", "domain": MCPT, "fields": ["name", "color"], **params}
        params = {key: value for key, value in params.items() if value is not None}
        return self.result("search", params, user=user)

    def test_search_respects_record_rules(self):
        result = self.search(order="color asc")
        self.assertEqual(set(result), {"records", "count", "truncated"})
        self.assertEqual(
            result["records"],
            [
                {"id": self.alpha.id, "name": "MCPT Alpha", "color": 3},
                {"id": self.beta.id, "name": "MCPT Beta", "color": 4},
            ],
        )
        self.assertEqual((result["count"], result["truncated"]), (2, False))
        admin = self.search(user="admin", order="color desc")
        self.assertEqual(admin["count"], 4)
        self.assertEqual(admin["records"][0]["name"], "MCPT Hidden Two")
        # Filtering on a hidden partner's value finds nothing.
        self.assertEqual(self.search(domain=[["color", "=", 200]])["count"], 0)
        self.assertEqual(self.search(domain=[["id", "=", self.hidden.id]])["records"], [])

    def test_limit_offset_and_truncation(self):
        page = self.search(user="admin", order="color asc", limit=1, offset=1)
        self.assertEqual([row["name"] for row in page["records"]], ["MCPT Beta"])
        self.assertEqual((page["count"], page["truncated"]), (4, True))
        self.assertEqual(self.search(user="admin", offset=1_000_000)["records"], [])
        self.assertEqual(self.search(user="admin", limit=None)["count"], 4)
        for params in (
            {"limit": 201},
            {"limit": 0},
            {"offset": -1},
            {"offset": 1_000_001},
            {"limit": True},
            {"offset": "1"},
        ):
            with self.subTest(params=params):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "fields": ["name"], **params}),
                    "invalid_params",
                )

    def test_paths_and_sub_domains(self):
        for domain in (
            [["category_id.name", "=", "MCP visible"]],
            [["category_id", "any", [["name", "=", "MCP visible"]]]],
            ["|", ["color", "=", 3], ["color", "in", [4, 100]]],
            ["!", ["color", ">", 3]],
        ):
            with self.subTest(domain=domain):
                result = self.search(user="admin", domain=MCPT + domain)
                self.assertGreaterEqual(result["count"], 1)
        self.assertEqual(
            self.search(domain=MCPT + [["color", "in", [3, 4, 100, 200]]])["count"], 2
        )

    def test_malformed_domains(self):
        for domain in (
            "name = 'x'",
            {"name": "x"},
            [["name", "=="]],
            [["name", "bogus", "x"]],
            [["id", "access", "read"]],
            [["name", "not =like", "x"]],
            [["partner_id", "any!", []]],
            [["name", "=", {"a": 1}]],
            [["name", "in", [["nested"]]]],
            [["nope", "=", 1]],
            [["name.x", "=", 1]],
            [["category_id.nope", "=", 1]],
            [["category_id", "any", "x"]],
            [["category_id", "any", [["nope", "=", 1]]]],
            [["Name", "=", "x"]],
            [["parent_id.parent_id.parent_id.parent_id.name", "=", "x"]],
            ["&"],
            [[1, "=", 1]],
            [["name", "=", "x" * 1001]],
        ):
            with self.subTest(domain=domain):
                message = self.assertApiError(
                    self.call("search", {"model": "res.partner", "domain": domain, "fields": ["name"]}),
                    "invalid_params",
                )
                self.assertNotIn("MCPT", message)

    def test_field_groups_in_fields_domain_and_order(self):
        with self.protected_field("res.partner", "function"):
            for params in (
                {"fields": ["name", "function"]},
                {"domain": [["function", "=", "Boss"]]},
                {"domain": [["category_id", "any", [["name", "!=", False]]], ["function", "!=", False]]},
                {"order": "function desc"},
            ):
                with self.subTest(params=params):
                    self.assertApiError(
                        self.call("search", {"model": "res.partner", "fields": ["name"], **params}),
                        "invalid_params",
                    )
            self.assertApiError(
                self.call("read", {"model": "res.partner", "ids": [self.alpha.id], "fields": ["function"]}),
                "invalid_params",
            )
            admin = self.search(user="admin", fields=["function"], order="function desc")
            self.assertEqual(admin["count"], 4)

    def test_denylist_applies_inside_domains_and_order_for_admins(self):
        set_config(self.env, 
            "o3p_mcp.api_denied_fields", "\n".join([*DEFAULT_DENIED_FIELDS, "color"])
        )
        for params in (
            {"fields": ["name", "color"]},
            {"domain": MCPT + [["color", ">", 100]]},
            {"order": "color desc"},
            {"domain": [["category_id", "any", [["color", "=", 0]]]]},
        ):
            with self.subTest(params=params):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "fields": ["name"], **params}, user="admin"),
                    "invalid_params",
                )
        # Fields and paths into hidden models or binary fields.
        for params in (
            {"model": "res.users", "fields": ["login"], "domain": [["api_key_ids.name", "=", "x"]]},
            {"model": "res.users", "fields": ["password"]},
            {"model": "res.users", "fields": ["login"], "domain": [["password", "!=", False]]},
            {"model": "res.partner", "fields": ["image_1920"]},
            {"model": "res.partner", "fields": ["name"], "domain": [["image_1920", "!=", False]]},
        ):
            with self.subTest(params=params):
                self.assertApiError(self.call("search", params, user="admin"), "invalid_params")
        self.assertApiError(
            self.call("search", {"model": "ir.config_parameter", "fields": ["key"]}, user="admin"),
            "unknown_model",
        )

    def test_unknown_parameters_are_refused(self):
        for op, params in (
            ("search", {"model": "res.partner", "fields": ["name"], "having": []}),
            ("search", {"model": "res.partner", "fields": ["name"], "context": {"active_test": False}}),
            ("read", {"model": "res.partner", "ids": [self.alpha.id], "fields": ["name"], "load": None}),
            ("describe", {"model": "res.partner", "fields": ["name"]}),
            ("models", {"model": "res.partner"}),
        ):
            with self.subTest(op=op, params=params):
                self.assertApiError(self.call(op, params), "invalid_params")

    def test_order_validation(self):
        self.assertEqual(
            [row["name"] for row in self.search(user="admin", order="color desc, name")["records"]][:1],
            ["MCPT Hidden Two"],
        )
        for order in ("color; drop table x", "color desc nulls first", "color descending", "nope", "display_name", "", 3, "category_id.name"):
            with self.subTest(order=order):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "fields": ["name"], "order": order}),
                    "invalid_params",
                )

    def test_read_direct_ids(self):
        result = self.result(
            "read", {"model": "res.partner", "ids": [self.beta.id, self.alpha.id], "fields": ["name"]}
        )
        self.assertEqual(
            result,
            {"records": [{"id": self.beta.id, "name": "MCPT Beta"}, {"id": self.alpha.id, "name": "MCPT Alpha"}]},
        )
        # Hidden and missing records are left out alike.
        for ids, expected in (
            ([self.alpha.id, self.hidden.id], [self.alpha.id]),
            ([self.hidden2.id], []),
            ([999999], []),
            ([self.hidden.id, 999999, self.beta.id], [self.beta.id]),
        ):
            with self.subTest(ids=ids):
                response = self.call("read", {"model": "res.partner", "ids": ids, "fields": ["name"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["id"] for row in response.json()["result"]["records"]], expected)
                self.assertNotIn("Hidden", response.text)
        self.assertEqual(
            len(self.result("read", {"model": "res.partner", "ids": [self.hidden.id], "fields": ["name"]}, user="admin")["records"]),
            1,
        )
        for ids in ([], [0], ["1"], [True], list(range(1, 102)), "1"):
            with self.subTest(ids=ids):
                self.assertApiError(
                    self.call("read", {"model": "res.partner", "ids": ids, "fields": ["name"]}),
                    "invalid_params",
                )

    def test_related_values_and_size_cap(self):
        rows = self.result(
            "read", {"model": "res.partner", "ids": [self.alpha.id], "fields": ["category_id", "create_date"]}
        )["records"]
        self.assertEqual(rows[0]["category_id"], [self.visible.id])
        self.assertIsInstance(rows[0]["create_date"], str)
        set_config(self.env, "o3p_mcp.api_max_response_bytes", 150)
        self.assertApiError(self.call("search", {"model": "res.partner", "domain": MCPT, "fields": ["name"]}, user="admin"), "result_too_large")


@tagged("post_install", "-at_install")
class TestApiGroup(O3pMcpApiCase):
    def group(self, user="restricted", **params):
        params = {"model": "res.partner", "domain": MCPT, **params}
        return self.result("group", params, user=user)

    def assertInvalid(self, user="restricted", **params):
        params = {"model": "res.partner", "domain": MCPT, **params}
        self.assertApiError(self.call("group", params, user=user), "invalid_params")

    def test_totals_never_include_hidden_rows(self):
        params = {"groupby": ["type"], "aggregates": ["__count", "color:sum", "color:max"]}
        self.assertEqual(
            self.group(**params),
            {"groups": [{"type": "contact", "__count": 2, "color:sum": 7, "color:max": 4}]},
        )
        self.assertEqual(
            self.group(user="admin", **params)["groups"],
            [{"type": "contact", "__count": 4, "color:sum": 307, "color:max": 200}],
        )
        # Grouping by a relation shows only groups of readable records.
        groups = self.group(groupby=["category_id"], aggregates=["__count"])["groups"]
        self.assertEqual(groups, [{"category_id": [self.visible.id, "MCP visible"], "__count": 2}])
        admin = self.group(user="admin", groupby=["category_id"], aggregates=["__count"])["groups"]
        self.assertEqual(sum(group["__count"] for group in admin), 4)

    def test_order_on_aggregates_and_limit(self):
        groups = self.group(
            user="admin", groupby=["name"], aggregates=["color:sum", "__count"],
            order="color:sum desc, __count desc", limit=2,
        )["groups"]
        self.assertEqual([group["name"] for group in groups], ["MCPT Hidden Two", "MCPT Hidden One"])
        groups = self.group(user="admin", groupby=["name"], aggregates=["__count"], order="name DESC")["groups"]
        self.assertEqual(groups[0]["name"], "MCPT Hidden Two")
        for order in ("color desc", "color:avg desc", "__count; drop", "name nulls first", 3):
            with self.subTest(order=order):
                self.assertInvalid(groupby=["name"], aggregates=["color:sum"], order=order)
        for limit in (0, 501, "5"):
            with self.subTest(limit=limit):
                self.assertInvalid(groupby=["name"], aggregates=["__count"], limit=limit)

    def test_granularity_and_counts(self):
        groups = self.group(groupby=["create_date:month"], aggregates=["__count", "name:count_distinct"])["groups"]
        self.assertEqual(sum(group["__count"] for group in groups), 2)
        self.assertEqual(groups[0]["name:count_distinct"], 2)
        self.assertEqual(len(groups[0]["create_date:month"]), 2)
        self.assertEqual(self.group(groupby=["create_date:hour"], aggregates=["__count"])["groups"][0]["__count"], 2)
        for groupby in (["create_date"], ["name:month"], ["create_date:decade"], ["display_name"], ["nope"], [], "name", ["a", "b", "c", "d", "e", "f"]):
            with self.subTest(groupby=groupby):
                self.assertInvalid(groupby=groupby, aggregates=["__count"])

    def test_aggregate_validation(self):
        for aggregates in (
            ["name:sum"],
            ["color:array_agg"],
            ["color:bool_and"],
            ["nope:sum"],
            ["display_name:count"],
            ["color"],
            ["color:sum:x"],
            [],
            "__count",
        ):
            with self.subTest(aggregates=aggregates):
                self.assertInvalid(groupby=["type"], aggregates=aggregates)
        self.assertInvalid(groupby=["type"])

    def test_field_groups_and_denylist(self):
        with self.protected_field("res.partner", "function"):
            for params in (
                {"groupby": ["function"], "aggregates": ["__count"]},
                {"groupby": ["type"], "aggregates": ["function:count"]},
                {"groupby": ["type"], "aggregates": ["__count"], "domain": [["function", "=", "x"]]},
            ):
                with self.subTest(params=params):
                    self.assertInvalid(**params)
            self.assertEqual(
                sum(g["__count"] for g in self.group(user="admin", groupby=["function"], aggregates=["__count"])["groups"]),
                4,
            )
        set_config(self.env, 
            "o3p_mcp.api_denied_fields", "\n".join([*DEFAULT_DENIED_FIELDS, "color"])
        )
        for params in (
            {"groupby": ["color"], "aggregates": ["__count"]},
            {"groupby": ["type"], "aggregates": ["color:sum"]},
            {"groupby": ["type"], "aggregates": ["__count"], "domain": [["color", ">", 0]]},
        ):
            with self.subTest(params=params):
                self.assertInvalid(user="admin", **params)


@tagged("post_install", "-at_install")
class TestApiX2manyConditions(O3pMcpApiCase):
    """Conditions on one2many and many2many fields apply the related rules.

    Odoo does not apply the related model's record rules to the short forms
    (``child_ids in [id]``, ``= id``, ``!= False``); the API rewrites them to
    ``any`` / ``not any``. Here a hidden contact belongs to a visible company.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.child = cls.env["res.partner"].create(
            {"name": "MCPT Hidden Child", "parent_id": cls.alpha.id}
        )

    def names(self, domain, user="restricted"):
        result = self.result(
            "search",
            {"model": "res.partner", "domain": MCPT + domain, "fields": ["name"]},
            user=user,
        )
        return sorted(row["name"] for row in result["records"])

    def test_short_forms_equal_the_explicit_any_form(self):
        child = self.child.id
        for short, explicit in (
            ([["child_ids", "in", [child]]], [["child_ids", "any", [["id", "in", [child]]]]]),
            ([["child_ids", "in", child]], [["child_ids", "any", [["id", "in", [child]]]]]),
            ([["child_ids", "=", child]], [["child_ids", "any", [["id", "=", child]]]]),
            ([["child_ids", "!=", False]], [["child_ids", "any", []]]),
            ([["child_ids", "=", False]], [["child_ids", "not any", []]]),
            ([["child_ids", "not in", [child]]], [["child_ids", "not any", [["id", "in", [child]]]]]),
            ([["child_ids", "!=", child]], [["child_ids", "not any", [["id", "=", child]]]]),
            (
                [["child_ids", "ilike", "Hidden Child"]],
                [["child_ids", "any", [["display_name", "ilike", "Hidden Child"]]]],
            ),
            (
                [["child_ids", "not ilike", "Hidden Child"]],
                [["child_ids", "not any", [["display_name", "ilike", "Hidden Child"]]]],
            ),
            ([["child_ids.name", "ilike", "Hidden"]], [["child_ids", "any", [["name", "ilike", "Hidden"]]]]),
            (
                [["parent_id.child_ids", "in", [child]]],
                [["parent_id", "any", [["child_ids", "any", [["id", "in", [child]]]]]]],
            ),
        ):
            with self.subTest(domain=short):
                self.assertEqual(self.names(short), self.names(explicit))
                self.assertEqual(self.names(short, "admin"), self.names(explicit, "admin"))

    def test_hidden_related_records_reveal_nothing(self):
        child = self.child.id
        for domain in (
            [["child_ids", "in", [child]]],
            [["child_ids", "=", child]],
            [["child_ids", "!=", False]],
            [["child_ids.color", ">=", 0]],
        ):
            with self.subTest(domain=domain):
                self.assertEqual(self.names(domain), [])
                self.assertEqual(self.names(domain, "admin"), ["MCPT Alpha"])
        groups = self.result(
            "group",
            {
                "model": "res.partner",
                "domain": MCPT + [["child_ids", "in", [child]]],
                "groupby": ["type"],
                "aggregates": ["__count"],
            },
        )["groups"]
        self.assertEqual(groups, [])

    def test_forms_without_a_faithful_rewrite_are_refused(self):
        for condition in (
            ["child_ids", "child_of", [self.alpha.id]],
            ["child_ids", "parent_of", [self.alpha.id]],
            ["child_ids", "<", 5],
            ["child_ids", "=?", False],
            ["child_ids", "in", [False]],
            ["child_ids", "in", ["x"]],
            ["child_ids", "=", True],
            ["child_ids", "=", "Hidden"],
        ):
            with self.subTest(condition=condition):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "domain": [condition], "fields": ["name"]}),
                    "invalid_params",
                )

    def test_related_model_must_be_readable(self):
        # Only Settings administrators may read res.users.log.
        for domain in (
            [["log_ids", "!=", False]],
            [["log_ids", "=", 1]],
            [["log_ids.create_date", ">", "2000-01-01"]],
            [["log_ids", "any", []]],
        ):
            with self.subTest(domain=domain):
                params = {"model": "res.users", "domain": domain, "fields": ["login"]}
                self.assertApiError(self.call("search", params), "access_denied")
                self.assertEqual(self.call("search", params, user="admin").status_code, 200)
