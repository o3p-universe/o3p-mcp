import json
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errors as pg_errors

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from .api_common import API_URL, O3pMcpApiCase, basic
from .common import REDIRECT_URI, RESOURCE


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
        params.set_str("o3p_mcp.resource_url", f"{RESOURCE}/")
        self.assertEqual(self.call("models").status_code, 200)
        params.set_str("o3p_mcp.resource_url", "https://other.example.com/mcp")
        self.assertApiError(self.call("models"), "invalid_token")
        params.set_str("o3p_mcp.resource_url", "")
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
        for op in ("sql", "write", "unlink", "nope"):
            with self.subTest(op=op):
                self.assertApiError(self.call(op, {"query": "select 1"}), "unknown_op")
        response = self.url_open(API_URL + "models")
        self.assertEqual(response.status_code, 405)
        response = self.url_open(
            API_URL + "models",
            method="OPTIONS",
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
        result = self.result("models", {"query": "partner"})
        names = [item["model"] for item in result]
        self.assertIn("res.partner", names)
        self.assertEqual(
            next(item for item in result if item["model"] == "res.partner")["name"], "Contact"
        )
        for item in result:
            self.assertEqual(set(item), {"model", "name"})
            self.assertTrue(self.env[item["model"]].with_user(self.restricted).has_access("read"))
        # A model only administrators may read.
        self.assertEqual(self.result("models", {"query": "ir.cron"}), [])
        admin = [item["model"] for item in self.result("models", {"query": "ir.cron"}, user="admin")]
        self.assertIn("ir.cron", admin)

    def test_models_never_lists_hidden_transient_or_abstract_models(self):
        for query in ("ir.config_parameter", "o3p.mcp", "apikeys", "mail_server", "res.config"):
            with self.subTest(query=query):
                names = [item["model"] for item in self.result("models", {"query": query}, user="admin")]
                self.assertFalse(
                    [name for name in names if name.startswith(("ir.config_parameter", "o3p.mcp", "res.users.apikeys", "ir.mail_server", "res.config"))],
                    names,
                )
        names = [item["model"] for item in self.result("models", {"query": "base"}, user="admin")]
        self.assertNotIn("base", names)

    def test_models_parameters(self):
        self.assertEqual(len(self.result("models", {"limit": 2}, user="admin")), 2)
        for params in ({"limit": 51}, {"limit": 0}, {"limit": "5"}, {"query": 3}, {"query": "x" * 201}):
            with self.subTest(params=params):
                self.assertApiError(self.call("models", params), "invalid_params")


@tagged("post_install", "-at_install")
class TestApiDescribe(O3pMcpApiCase):
    def describe(self, model, user="restricted"):
        return self.result("describe", {"model": model}, user=user)

    def test_describe_shape(self):
        result = self.describe("res.partner")
        self.assertEqual(set(result), {"model", "name", "fields"})
        self.assertEqual(result["model"], "res.partner")
        fields_by_name = {item["name"]: item for item in result["fields"]}
        self.assertEqual(fields_by_name["color"]["type"], "integer")
        self.assertEqual(fields_by_name["category_id"]["relation"], "res.partner.category")
        self.assertIn("selection", fields_by_name["type"])
        for item in result["fields"]:
            self.assertLessEqual(
                set(item),
                {"name", "string", "type", "relation", "required", "readonly", "selection", "help"},
            )
            self.assertNotEqual(item["type"], "binary")

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
        params.set_str("o3p_mcp.api_denied_models", "res.partner*")
        params.set_str("o3p_mcp.api_denied_fields", "color\n*phone*")
        self.assertApiError(self.call("describe", {"model": "res.partner"}), "unknown_model")
        names = {item["name"] for item in self.describe("res.users", user="admin")["fields"]}
        self.assertNotIn("phone", names)
        # Replacing the default list exposes what it used to hide.
        self.assertIn("password", names)
        params.set_str("o3p_mcp.api_denied_models", "")
        self.assertApiError(
            self.call("describe", {"model": "ir.config_parameter"}, user="admin"), "unknown_model"
        )


@tagged("post_install", "-at_install")
class TestApiLimitsAndAudit(O3pMcpApiCase):
    def test_result_too_large(self):
        self.env["ir.config_parameter"].sudo().set_int("o3p_mcp.api_max_response_bytes", 200)
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
