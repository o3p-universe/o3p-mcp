import re
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged

from ..models.oauth import OAuthError, get_config, get_config_int, set_config
from ..models.res_config_settings import health_url
from .common import RESOURCE, O3pMcpCommon


SECRET_RE = re.compile(r"Client ID: (\S+) — Client secret: (\S+) —")


@tagged("post_install", "-at_install")
class TestSettings(O3pMcpCommon, TransactionCase):
    def settings(self):
        return self.env["res.config.settings"].create({})

    def rotate(self):
        action = self.settings().action_o3p_mcp_rotate_credentials()
        self.assertEqual(action["tag"], "display_notification")
        self.assertTrue(action["params"]["sticky"])
        client_id, secret = SECRET_RE.search(action["params"]["message"]).groups()
        return client_id, secret

    def test_values_are_stored_as_config_parameters(self):
        params = self.env["ir.config_parameter"]
        self.assertEqual(get_config_int(params.env, "o3p_mcp.access_token_minutes"), 60)
        self.assertEqual(get_config_int(params.env, "o3p_mcp.refresh_token_days"), 30)
        with Form(self.env["res.config.settings"]) as form:
            form.o3p_mcp_resource_url = f" {RESOURCE} "
            form.o3p_mcp_access_token_minutes = 15
            form.o3p_mcp_refresh_token_days = 7
        form.record.execute()
        self.assertEqual(get_config(params.env, "o3p_mcp.resource_url"), RESOURCE)
        self.assertEqual(get_config_int(params.env, "o3p_mcp.access_token_minutes"), 15)
        self.assertEqual(get_config_int(params.env, "o3p_mcp.refresh_token_days"), 7)
        oauth = self.env["o3p.mcp.oauth"]
        self.assertEqual(oauth._resource_url(), RESOURCE)
        self.assertEqual(oauth._access_token_lifetime().total_seconds(), 15 * 60)
        self.assertEqual(oauth._refresh_token_lifetime().days, 7)
        _token, response = self.Token._issue(self.client, self.editor, {"okf.read"}, RESOURCE)
        self.assertEqual(response["expires_in"], 900)
        settings = self.settings()
        self.assertEqual(settings.o3p_mcp_resource_url, RESOURCE)
        self.assertEqual(settings.o3p_mcp_access_token_minutes, 15)
        issuer = oauth._issuer()
        self.assertEqual(
            settings.o3p_mcp_introspection_url, f"{issuer}/o3p_mcp/oauth/introspect"
        )
        self.assertEqual(
            settings.o3p_mcp_metadata_url,
            f"{issuer}/.well-known/oauth-authorization-server",
        )

    def test_issuer(self):
        params = self.env["ir.config_parameter"]
        oauth = self.env["o3p.mcp.oauth"]
        # Initialised from web.base.url at install.
        self.assertTrue(get_config(params.env, "o3p_mcp.issuer"))
        set_config(params.env, "o3p_mcp.issuer", "https://odoo.example.com")
        set_config(params.env, "web.base.url", "https://elsewhere.example.com")
        oauth._init_issuer()
        self.assertEqual(oauth._issuer(), "https://odoo.example.com")
        set_config(params.env, "o3p_mcp.issuer", "")
        oauth._init_issuer()
        self.assertEqual(oauth._issuer(), "https://elsewhere.example.com")
        for issuer in (
            "http://odoo.example.com",
            "https://odoo.example.com/odoo",
            "https://odoo.example.com?x=1",
            "https://user@odoo.example.com",
            "ftp://odoo.example.com",
            "odoo.example.com",
            "",
        ):
            with self.subTest(issuer=issuer), self.assertRaises(UserError):
                self.env["res.config.settings"].create({"o3p_mcp_issuer": issuer}).execute()
        for issuer, stored in (
            ("https://odoo.example.com/", "https://odoo.example.com"),
            ("http://localhost:8069", "http://localhost:8069"),
            ("https://odoo.example.com:8443", "https://odoo.example.com:8443"),
        ):
            self.env["res.config.settings"].create({"o3p_mcp_issuer": issuer}).execute()
            self.assertEqual(get_config(params.env, "o3p_mcp.issuer"), stored)
        # An inherited value that would be refused does not block other settings.
        set_config(params.env, "o3p_mcp.issuer", "http://odoo.example.com")
        self.env["res.config.settings"].create({"o3p_mcp_access_token_minutes": 30}).execute()
        self.assertEqual(get_config_int(params.env, "o3p_mcp.access_token_minutes"), 30)
        self.assertEqual(oauth._issuer(), "http://odoo.example.com")

    def test_invalid_values_are_refused(self):
        for values in (
            {"o3p_mcp_resource_url": "ftp://mcp.example.com/mcp"},
            {"o3p_mcp_resource_url": "https://mcp.example.com/mcp#x"},
            {"o3p_mcp_resource_url": "mcp.example.com"},
            {"o3p_mcp_access_token_minutes": 0},
            {"o3p_mcp_access_token_minutes": 1441},
            {"o3p_mcp_refresh_token_days": 0},
            {"o3p_mcp_refresh_token_days": 366},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                self.env["res.config.settings"].create(values).execute()

    def test_credential_rotation_invalidates_the_old_secret(self):
        Client = self.env["o3p.mcp.client"]
        self.assertFalse(self.settings().o3p_mcp_resource_server_client_id)
        client_id, first = self.rotate()
        server = Client._authenticate(
            client_id, first, "client_secret_basic", kind="resource_server"
        )
        self.assertEqual(server.kind, "resource_server")
        self.assertNotIn(first, server.secret_hash)
        self.assertEqual(self.settings().o3p_mcp_resource_server_client_id, client_id)
        same_id, second = self.rotate()
        self.assertEqual(same_id, client_id)
        self.assertNotEqual(second, first)
        with self.assertRaises(OAuthError):
            Client._authenticate(
                client_id, first, "client_secret_basic", kind="resource_server"
            )
        Client._authenticate(client_id, second, "client_secret_basic", kind="resource_server")
        self.assertEqual(Client.search_count([("kind", "=", "resource_server")]), 1)
        # An archived resource server is reactivated by the next rotation.
        server.active = False
        rotated_id, _third = self.rotate()
        self.assertEqual(rotated_id, client_id)
        self.assertTrue(server.active)

    def test_only_administrators_rotate(self):
        settings_user = new_test_user(
            self.env, "o3p_mcp_settings", groups="base.group_user,base.group_erp_manager"
        )
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(
                settings_user
            ).new().action_o3p_mcp_rotate_credentials()

    def test_health_check(self):
        self.assertEqual(
            health_url("https://mcp.example.com/mcp"), "https://mcp.example.com/health"
        )
        self.assertEqual(
            health_url("https://host.example.com/okf/mcp/"),
            "https://host.example.com/okf/health",
        )
        self.assertEqual(health_url("https://mcp.example.com"), "https://mcp.example.com/health")
        settings = self.settings()
        with self.assertRaises(UserError):
            settings.action_o3p_mcp_check_health()
        settings.o3p_mcp_resource_url = RESOURCE
        target = "odoo.addons.o3p_mcp.models.res_config_settings.requests.get"
        with patch(target) as get:
            get.return_value.ok = True
            get.return_value.status_code = 200
            action = settings.action_o3p_mcp_check_health()
        get.assert_called_once_with("https://mcp.example.com/health", timeout=10)
        self.assertEqual(action["params"]["type"], "success")
        with patch(target, side_effect=requests.ConnectionError("refused")):
            action = settings.action_o3p_mcp_check_health()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("ConnectionError", action["params"]["message"])

    def test_data_api_settings(self):
        params = self.env["ir.config_parameter"]
        settings = self.settings()
        self.assertIn("o3p.mcp.*", settings.o3p_mcp_api_denied_models.splitlines())
        self.assertIn("*password*", settings.o3p_mcp_api_denied_fields.splitlines())
        self.assertEqual(settings.o3p_mcp_api_max_response_kb, 1000)
        self.env["res.config.settings"].create(
            {
                "o3p_mcp_api_denied_models": "o3p.mcp.*, ir.config_parameter\nres.bank",
                "o3p_mcp_api_denied_fields": "",
                "o3p_mcp_api_max_response_kb": 50,
                "o3p_mcp_api_timeout_seconds": 5,
                "o3p_mcp_api_log_days": 7,
            }
        ).execute()
        self.assertEqual(
            get_config(params.env, "o3p_mcp.api_denied_models"), "o3p.mcp.*\nir.config_parameter\nres.bank"
        )
        # An empty list restores the default.
        self.assertIn("*password*", get_config(params.env, "o3p_mcp.api_denied_fields"))
        self.assertEqual(get_config_int(params.env, "o3p_mcp.api_max_response_bytes"), 50_000)
        self.assertEqual(get_config_int(params.env, "o3p_mcp.api_timeout_seconds"), 5)
        self.assertEqual(get_config_int(params.env, "o3p_mcp.api_log_days"), 7)
        api = self.env["o3p.mcp.api"]
        self.assertTrue(api._model_denied("res.bank"))
        self.assertEqual(api._timeout_seconds(), 5)
        for values in (
            {"o3p_mcp_api_max_response_kb": 0},
            {"o3p_mcp_api_timeout_seconds": 301},
            {"o3p_mcp_api_log_days": 0},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                self.env["res.config.settings"].create(values).execute()

    def test_views_load(self):
        views = self.env["o3p.mcp.api.log"].get_views([(False, "list"), (False, "search")])
        self.assertEqual(set(views["views"]), {"list", "search"})
        for model in ("o3p.mcp.client", "o3p.mcp.token"):
            with self.subTest(model=model):
                views = self.env[model].get_views(
                    [(False, "list"), (False, "form"), (False, "search")]
                )
                self.assertEqual(set(views["views"]), {"list", "form", "search"})
        arch = self.env["res.config.settings"].get_views([(False, "form")])["views"][
            "form"
        ]["arch"]
        for name in (
            "o3p_mcp_resource_url",
            "o3p_mcp_access_token_minutes",
            "o3p_mcp_refresh_token_days",
            "action_o3p_mcp_rotate_credentials",
            "action_o3p_mcp_check_health",
            "o3p_mcp_api_denied_models",
            "o3p_mcp_api_denied_fields",
            "o3p_mcp_api_timeout_seconds",
        ):
            self.assertIn(name, arch)
        for xmlid in (
            "o3p_mcp.menu_o3p_mcp_client",
            "o3p_mcp.menu_o3p_mcp_token",
            "o3p_mcp.action_mcp_token_revoke",
            "o3p_mcp.ir_cron_purge_expired",
        ):
            self.assertTrue(self.env.ref(xmlid))

    def test_client_form_edits_redirect_uris(self):
        with Form(self.env["o3p.mcp.client"]) as form:
            form.name = "Desktop client"
            form.redirect_uris_text = (
                "http://127.0.0.1:33418/callback\n\n  https://app.example.com/cb  \n"
            )
        self.assertEqual(
            form.record.redirect_uris,
            ["http://127.0.0.1:33418/callback", "https://app.example.com/cb"],
        )
        with self.assertRaises(ValidationError), Form(form.record) as edit:
            edit.redirect_uris_text = "javascript:alert(1)"

    def test_revoke_actions(self):
        token, response = self.Token._issue(
            self.client, self.editor, {"okf.read"}, RESOURCE
        )
        other, other_response = self.Token._issue(
            self.client, self.reader, {"okf.read"}, RESOURCE
        )
        action = self.env.ref("o3p_mcp.action_mcp_token_revoke")
        action.with_context(
            active_model="o3p.mcp.token", active_ids=token.ids, active_id=token.id
        ).run()
        self.assertTrue(token.revoked_at)
        self.assertFalse(other.revoked_at)
        self.client.action_revoke_tokens()
        self.assertTrue(other.revoked_at)
        self.assertEqual(self.Token._introspect(other_response["access_token"]), {"active": False})
        with self.assertRaises(AccessError):
            other.with_user(self.editor).action_revoke()
