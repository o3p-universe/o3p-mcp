import json
import time
from pathlib import Path

from odoo.tests import tagged

from .common import REDIRECT_URI, RESOURCE, LogCapture
from .test_token import INACTIVE, O3pMcpTokenCase, basic


INTROSPECT_URL = "/o3p_mcp/oauth/introspect"
REVOKE_URL = "/o3p_mcp/oauth/revoke"
FIXTURE = Path(__file__).parent / "fixtures" / "introspection_active.json"


class O3pMcpResourceServerCase(O3pMcpTokenCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.env["o3p.mcp.client"].create(
            {
                "name": "MCP server",
                "kind": "resource_server",
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        cls.server_secret = cls.server._set_new_secret()

    def introspect(self, token, headers=None):
        if headers is None:
            headers = basic(self.server.client_id, self.server_secret)
        data = {} if token is None else {"token": token}
        return self.url_open(INTROSPECT_URL, data=data or {"x": ""}, headers=headers)

    def revoke(self, token, **data):
        data.setdefault("client_id", self.client.client_id)
        headers = data.pop("headers", None)
        return self.url_open(REVOKE_URL, data=dict(data, token=token), headers=headers)

    def answer(self, token):
        response = self.introspect(token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json()


@tagged("post_install", "-at_install")
class TestIntrospection(O3pMcpResourceServerCase):
    def test_response_follows_the_shared_contract(self):
        expected = json.loads(FIXTURE.read_text())
        tokens = self.tokens_for()
        response = self.introspect(tokens["access_token"])
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        answer = response.json()
        self.assertEqual(list(answer), list(expected))
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertIs(type(answer[key]), type(value))
        self.assertIs(answer["active"], True)
        self.assertEqual(answer["sub"], str(self.editor.id))
        self.assertEqual(answer["username"], self.editor.login)
        self.assertEqual(answer["name"], self.editor.name)
        self.assertEqual(answer["email"], self.editor.email)
        self.assertEqual(answer["scope"], "okf.read okf.write")
        self.assertEqual(answer["aud"], RESOURCE)
        self.assertGreater(answer["exp"], time.time())
        self.assertLessEqual(answer["exp"], time.time() + 3600)
        self.assertEqual(answer["client_id"], self.client.client_id)

    def test_wrong_credentials(self):
        tokens = self.tokens_for()
        token = tokens["access_token"]
        for headers in (
            {},
            basic(self.server.client_id, "wrong"),
            basic("c_unknown", self.server_secret),
            basic(self.client.client_id, ""),
            {"Authorization": f"Bearer {token}"},
        ):
            with self.subTest(headers=list(headers)):
                response = self.introspect(token, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"], "invalid_client")
                self.assertEqual(
                    response.headers["WWW-Authenticate"], 'Basic realm="o3p_mcp"'
                )
                self.assertNotIn(token, response.text)
        # Resource-server credentials in the body are not accepted either.
        response = self.url_open(
            INTROSPECT_URL,
            data={
                "token": token,
                "client_id": self.server.client_id,
                "client_secret": self.server_secret,
            },
        )
        self.assertEqual(response.status_code, 401)
        # An MCP client cannot introspect.
        confidential = self.env["o3p.mcp.client"].create(
            {
                "name": "Confidential",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        secret = confidential._set_new_secret()
        response = self.introspect(token, headers=basic(confidential.client_id, secret))
        self.assertEqual(response.status_code, 401)

    def test_rotated_server_secret(self):
        tokens = self.tokens_for()
        old_secret = self.server_secret
        new_secret = self.server._set_new_secret()
        response = self.introspect(
            tokens["access_token"], headers=basic(self.server.client_id, old_secret)
        )
        self.assertEqual(response.status_code, 401)
        response = self.introspect(
            tokens["access_token"], headers=basic(self.server.client_id, new_secret)
        )
        self.assertTrue(response.json()["active"])

    def test_inactive_answers(self):
        tokens = self.tokens_for()
        self.assertEqual(self.answer("not-a-token"), INACTIVE)
        self.assertEqual(self.answer(None), INACTIVE)
        self.assertEqual(self.answer(tokens["refresh_token"]), INACTIVE)
        self.assertEqual(self.revoke(tokens["access_token"]).status_code, 200)
        self.assertEqual(self.answer(tokens["access_token"]), INACTIVE)

    def test_scope_grows_with_odoo_scopes(self):
        tokens = self.tokens_for(scope=None)
        answer = self.answer(tokens["access_token"])
        self.assertEqual(answer["scope"], "okf.read okf.write odoo.read odoo.sql")
        self.assertEqual(
            list(answer), list(json.loads(FIXTURE.read_text()))
        )
        self.demote_to_reader(self.editor)
        self.assertEqual(
            self.answer(tokens["access_token"])["scope"], "okf.read odoo.read odoo.sql"
        )

    def test_editor_group_removed(self):
        tokens = self.tokens_for()
        self.demote_to_reader(self.editor)
        self.assertEqual(self.answer(tokens["access_token"])["scope"], "okf.read")
        self.remove_mcp_access(self.editor)
        self.assertEqual(self.answer(tokens["access_token"]), INACTIVE)


@tagged("post_install", "-at_install")
class TestRevocation(O3pMcpResourceServerCase):
    def test_revoking_either_token_revokes_the_family(self):
        for hint, kind in (
            (None, "access_token"),
            ("refresh_token", "refresh_token"),
            ("access_token", "refresh_token"),
        ):
            with self.subTest(hint=hint, kind=kind):
                first = self.tokens_for()
                second = self.refresh(first["refresh_token"]).json()
                data = {"token_type_hint": hint} if hint else {}
                response = self.revoke(second[kind], **data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(self.answer(first["access_token"]), INACTIVE)
                self.assertEqual(self.answer(second["access_token"]), INACTIVE)
                self.assertEqual(
                    self.refresh(second["refresh_token"]).json()["error"],
                    "invalid_grant",
                )

    def test_other_clients_and_unknown_tokens(self):
        tokens = self.tokens_for()
        other = self.env["o3p.mcp.client"].create(
            {"name": "Other", "redirect_uris": [REDIRECT_URI]}
        )
        response = self.revoke(tokens["access_token"], client_id=other.client_id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.answer(tokens["access_token"])["active"])
        self.assertEqual(self.revoke("not-a-token").status_code, 200)
        response = self.revoke(tokens["access_token"], client_id="c_unknown")
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.answer(tokens["access_token"])["active"])

    def test_cors(self):
        response = self.url_open(
            REVOKE_URL,
            method="OPTIONS",
            headers={
                "Origin": "https://inspector.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(
            self.revoke("x").headers["Access-Control-Allow-Origin"], "*"
        )


@tagged("post_install", "-at_install")
class TestNoSecretsInLogs(O3pMcpResourceServerCase):
    def test_flow_logs_no_secret(self):
        with LogCapture() as capture:
            registered = self.url_open(
                "/o3p_mcp/oauth/register",
                json={"redirect_uris": [REDIRECT_URI], "client_name": "Logged"},
            ).json()
            code = self.authorize_code()
            first = self.exchange(code).json()
            second = self.refresh(first["refresh_token"]).json()
            self.refresh(first["refresh_token"])  # replay
            self.exchange(code)  # reuse
            self.refresh("not-a-token")
            third = self.tokens_for()
            self.answer(third["access_token"])
            self.introspect(third["access_token"], headers=basic(self.server.client_id, "x"))
            self.revoke(third["refresh_token"])
            self.answer(third["access_token"])
        self.assertTrue(capture.messages)
        logged = "\n".join(capture.messages)
        for secret in (
            registered["client_secret"],
            self.server_secret,
            code,
            self.verifier,
            first["access_token"],
            first["refresh_token"],
            second["access_token"],
            second["refresh_token"],
            third["access_token"],
            third["refresh_token"],
        ):
            self.assertNotIn(secret, logged)
