import base64
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from .common import REDIRECT_URI, RESOURCE, O3pMcpHttpCase, location_of


TOKEN_URL = "/o3p_mcp/oauth/token"
INACTIVE = {"active": False}


def basic(client_id, secret):
    raw = f"{client_id}:{secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


class O3pMcpTokenCase(O3pMcpHttpCase):
    def post_token(self, data, headers=None):
        return self.url_open(TOKEN_URL, data=data, headers=headers)

    def exchange(self, code, **overrides):
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": self.verifier,
            "client_id": self.client.client_id,
            "resource": RESOURCE,
        }
        data.update(overrides)
        headers = data.pop("headers", None)
        return self.post_token(
            {key: value for key, value in data.items() if value is not None}, headers
        )

    def refresh(self, refresh_token, **overrides):
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client.client_id,
        }
        data.update(overrides)
        headers = data.pop("headers", None)
        return self.post_token(
            {key: value for key, value in data.items() if value is not None}, headers
        )

    def tokens_for(self, login="o3p_mcp_editor", **overrides):
        response = self.exchange(self.authorize_code(login, **overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assertTokenError(self, response, error, status=400):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"], error)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("access_token", response.json())


@tagged("post_install", "-at_install")
class TestTokenEndpoint(O3pMcpTokenCase):
    def test_code_flow(self):
        response = self.exchange(self.authorize_code())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        data = response.json()
        self.assertEqual(
            set(data),
            {"access_token", "token_type", "expires_in", "refresh_token", "scope"},
        )
        self.assertEqual(data["token_type"], "Bearer")
        self.assertEqual(data["expires_in"], 3600)
        self.assertEqual(data["scope"], "okf.read okf.write")
        answer = self.Token._introspect(data["access_token"])
        self.assertEqual(answer["sub"], str(self.editor.id))
        self.assertEqual(answer["aud"], RESOURCE)
        self.assertEqual(answer["client_id"], self.client.client_id)
        refreshed = self.refresh(data["refresh_token"])
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertNotEqual(refreshed.json()["refresh_token"], data["refresh_token"])

    def test_reader_gets_read_only(self):
        data = self.tokens_for("o3p_mcp_reader")
        self.assertEqual(data["scope"], "okf.read")

    def test_pkce_mismatch_consumes_the_code(self):
        code = self.authorize_code()
        self.assertTokenError(
            self.exchange(code, code_verifier="x" * 43), "invalid_grant"
        )
        self.assertTokenError(self.exchange(code), "invalid_grant")
        self.assertTokenError(
            self.exchange(self.authorize_code(), code_verifier=None), "invalid_grant"
        )

    def test_code_reuse_revokes_the_issued_tokens(self):
        code = self.authorize_code()
        first = self.exchange(code).json()
        self.assertTrue(self.Token._introspect(first["access_token"])["active"])
        self.assertTokenError(self.exchange(code), "invalid_grant")
        self.assertEqual(self.Token._introspect(first["access_token"]), INACTIVE)
        self.assertTokenError(self.refresh(first["refresh_token"]), "invalid_grant")

    def test_redirect_uri_must_match(self):
        self.assertTokenError(
            self.exchange(self.authorize_code(), redirect_uri=f"{REDIRECT_URI}/x"),
            "invalid_grant",
        )
        # The only registered redirect URI may be left out, as at authorization.
        response = self.exchange(self.authorize_code(), redirect_uri=None)
        self.assertEqual(response.status_code, 200, response.text)

    def test_loopback_port_may_change(self):
        # Review probe: native clients listen on a port chosen at run time.
        self.client.redirect_uris = ["http://127.0.0.1:33418/callback"]
        uri = "http://127.0.0.1:50123/callback"
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        response = self.decide("approve", redirect_uri=uri)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["Location"].startswith(f"{uri}?code="))
        code = location_of(response)[1]["code"]
        self.assertEqual(self.Code.search([]).redirect_uri, uri)
        self.assertEqual(self.exchange(code, redirect_uri=uri).status_code, 200)
        code = self.authorize_code(redirect_uri=uri)
        self.assertEqual(
            self.exchange(code, redirect_uri="http://127.0.0.1:50999/callback").status_code,
            200,
        )
        code = self.authorize_code(redirect_uri=uri)
        self.assertTokenError(
            self.exchange(code, redirect_uri="http://127.0.0.1:50123/other"),
            "invalid_grant",
        )
        for other in (
            "http://127.0.0.1:50123/other",
            "http://localhost:50123/callback",
            "http://127.0.0.1:50123/callback?x=1",
        ):
            with self.subTest(other=other):
                page = self.get_consent(redirect_uri=other)
                self.assertEqual(page.status_code, 400)
                self.assertNotIn("Location", page.headers)

    def test_other_ports_are_exact_elsewhere(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        page = self.get_consent(redirect_uri="https://client.example.com:8443/callback")
        self.assertEqual(page.status_code, 400)
        self.assertNotIn("Location", page.headers)

    def test_expired_code(self):
        code = self.authorize_code()
        self.Code.search([]).expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertTokenError(self.exchange(code), "invalid_grant")

    def test_code_is_bound_to_its_client(self):
        other = self.env["o3p.mcp.client"].create(
            {"name": "Other", "redirect_uris": [REDIRECT_URI]}
        )
        code = self.authorize_code()
        self.assertTokenError(
            self.exchange(code, client_id=other.client_id), "invalid_grant"
        )
        self.assertEqual(self.exchange(code).status_code, 200)

    def test_resource_must_match(self):
        self.assertTokenError(
            self.exchange(self.authorize_code(), resource="https://other.example.com/mcp"),
            "invalid_target",
        )

    def test_refresh_rotation_and_replay(self):
        first = self.tokens_for()
        second = self.refresh(first["refresh_token"]).json()
        self.assertTokenError(self.refresh(first["refresh_token"]), "invalid_grant")
        self.assertEqual(self.Token._introspect(second["access_token"]), INACTIVE)
        self.assertTokenError(self.refresh(second["refresh_token"]), "invalid_grant")

    def test_refresh_can_narrow_scope(self):
        first = self.tokens_for()
        narrowed = self.refresh(first["refresh_token"], scope="okf.read")
        self.assertEqual(narrowed.json()["scope"], "okf.read")
        self.assertTokenError(
            self.refresh(narrowed.json()["refresh_token"], scope="okf.read okf.write"),
            "invalid_scope",
        )

    def test_refresh_scope_without_read_keeps_the_grant(self):
        # Review probe: scope=okf.write alone used to revoke the family.
        tokens = self.tokens_for()
        self.assertTokenError(
            self.refresh(tokens["refresh_token"], scope="okf.write"), "invalid_scope"
        )
        self.assertTrue(self.Token._introspect(tokens["access_token"])["active"])
        response = self.refresh(tokens["refresh_token"], scope="okf.read")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["scope"], "okf.read")

    def test_group_removed_between_refreshes(self):
        first = self.tokens_for()
        self.demote_to_reader(self.editor)
        second = self.refresh(first["refresh_token"])
        self.assertEqual(second.json()["scope"], "okf.read")
        self.remove_mcp_access(self.editor)
        self.assertTokenError(self.refresh(second.json()["refresh_token"]), "invalid_grant")

    def test_user_removed_before_exchange(self):
        code = self.authorize_code()
        self.remove_mcp_access(self.editor)
        self.assertTokenError(self.exchange(code), "invalid_grant")

    def test_unknown_client_and_grant_type(self):
        self.assertTokenError(
            self.refresh("x", client_id="c_unknown"), "invalid_client", status=401
        )
        self.assertTokenError(self.post_token({"client_id": self.client.client_id}), "invalid_request")
        self.assertTokenError(
            self.post_token(
                {"grant_type": "client_credentials", "client_id": self.client.client_id}
            ),
            "unsupported_grant_type",
        )

    def test_confidential_clients(self):
        Client = self.env["o3p.mcp.client"]
        basic_client = Client.create(
            {
                "name": "Basic",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        secret = basic_client._set_new_secret()
        self.client = basic_client
        code = self.authorize_code()
        wrong = self.exchange(code, client_id=None, headers=basic(basic_client.client_id, "x"))
        self.assertTokenError(wrong, "invalid_client", status=401)
        self.assertEqual(wrong.headers["WWW-Authenticate"], 'Basic realm="o3p_mcp"')
        self.assertTokenError(
            self.exchange(code, client_secret=secret), "invalid_client", status=401
        )
        self.assertTokenError(
            self.exchange(code, headers=basic(basic_client.client_id, secret), client_secret=secret),
            "invalid_request",
        )
        response = self.exchange(code, client_id=None, headers=basic(basic_client.client_id, secret))
        self.assertEqual(response.status_code, 200, response.text)

        post_client = Client.create(
            {
                "name": "Post",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "client_secret_post",
            }
        )
        post_secret = post_client._set_new_secret()
        self.client = post_client
        code = self.authorize_code()
        self.assertTokenError(self.exchange(code), "invalid_client", status=401)
        response = self.exchange(code, client_secret=post_secret)
        self.assertEqual(response.status_code, 200, response.text)

    def test_cors_and_methods(self):
        preflight = self.url_open(
            TOKEN_URL,
            method="OPTIONS",
            headers={
                "Origin": "https://inspector.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("Authorization", preflight.headers["Access-Control-Allow-Headers"])
        self.assertEqual(self.url_open(TOKEN_URL).status_code, 405)
