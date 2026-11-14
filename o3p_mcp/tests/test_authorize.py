import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit

from odoo.tests import tagged

from .common import AUTHORIZE_URL, REDIRECT_URI, RESOURCE, O3pMcpHttpCase, location_of


@tagged("post_install", "-at_install")
class TestAuthorize(O3pMcpHttpCase):
    def assertClientError(self, response, error):
        self.assertEqual(response.status_code, 303, response.text[:500])
        uri, query = location_of(response)
        self.assertEqual(uri, REDIRECT_URI)
        self.assertEqual(query["error"], error)
        self.assertEqual(query["state"], "state-123")
        self.assertEqual(query["iss"], self.issuer)
        self.assertNotIn("code", query)

    def assertErrorPage(self, response, status=400):
        self.assertEqual(response.status_code, status)
        self.assertNotIn("Location", response.headers)
        self.assertIn("Authorization failed", response.text)

    def test_anonymous_visit_redirects_to_login(self):
        response = self.get_consent()
        self.assertEqual(response.status_code, 303)
        location = urlsplit(response.headers["Location"])
        self.assertEqual(location.path, "/web/login")
        redirect = dict(parse_qsl(location.query))["redirect"]
        self.assertTrue(redirect.startswith(f"{AUTHORIZE_URL}?"))
        self.assertEqual(
            dict(parse_qsl(urlsplit(redirect).query)), self.authorization_params()
        )

    def test_consent_page(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        response = self.get_consent()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(
            response.headers["Content-Security-Policy"], "frame-ancestors 'none'"
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for text in (
            "Test MCP client",
            "MCP Editor",
            RESOURCE,
            "okf.read",
            "okf.write",
            "client.example.com",
            'name="csrf_token"',
            f'value="{self.challenge}"',
        ):
            self.assertIn(text, response.text)

    def test_reader_is_offered_read_only(self):
        self.authenticate("o3p_mcp_reader", "o3p_mcp_reader")
        response = self.get_consent()
        self.assertEqual(response.status_code, 200)
        self.assertIn("okf.read", response.text)
        self.assertNotIn("<code>okf.write</code>", response.text)

    def test_pkce_is_required(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertClientError(self.get_consent(code_challenge=None), "invalid_request")
        self.assertClientError(
            self.get_consent(code_challenge_method=None), "invalid_request"
        )
        self.assertClientError(
            self.get_consent(code_challenge_method="plain"), "invalid_request"
        )
        self.assertClientError(
            self.get_consent(code_challenge="too-short"), "invalid_request"
        )

    def test_resource_must_be_the_mcp_server(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertClientError(
            self.get_consent(resource="https://other.example.com/mcp"), "invalid_target"
        )
        self.assertEqual(self.get_consent(resource=f"{RESOURCE}/").status_code, 200)
        self.assertEqual(self.get_consent(resource=None).status_code, 200)

    def test_unconfigured_resource_shows_an_error(self):
        self.env["ir.config_parameter"].sudo().set_str("o3p_mcp.resource_url", "")
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertErrorPage(self.get_consent())

    def test_response_type_and_scope(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertClientError(
            self.get_consent(response_type="token"), "unsupported_response_type"
        )
        self.assertClientError(self.get_consent(scope="okf.admin"), "invalid_scope")
        self.assertClientError(self.get_consent(scope="okf.write"), "invalid_scope")

    def test_untrusted_client_or_redirect_uri_never_redirects(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        for overrides in (
            {"client_id": "c_unknown"},
            {"client_id": None},
            {"redirect_uri": "https://attacker.example.com/callback"},
            {"redirect_uri": f"{REDIRECT_URI}/more"},
            {"redirect_uri": f"{REDIRECT_URI}?x=1"},
            {"redirect_uri": REDIRECT_URI.upper()},
        ):
            with self.subTest(overrides=overrides):
                self.assertErrorPage(self.get_consent(**overrides))
        self.client.active = False
        self.assertErrorPage(self.get_consent())

    def test_single_registered_redirect_uri_may_be_omitted(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertEqual(self.get_consent(redirect_uri=None).status_code, 200)
        self.client.redirect_uris = [REDIRECT_URI, "https://client.example.com/other"]
        self.assertErrorPage(self.get_consent(redirect_uri=None))

    def test_user_without_mcp_group(self):
        self.authenticate("o3p_mcp_plain", "o3p_mcp_plain")
        response = self.get_consent()
        self.assertErrorPage(response, status=403)
        self.assertIn("error=access_denied", response.text)

    def test_user_without_mcp_group_is_never_redirected(self):
        # Review probe: a self-registered client sending a plain user straight
        # back to an arbitrary site with a bare client_id.
        registered = self.url_open(
            "/o3p_mcp/oauth/register",
            json={
                "redirect_uris": ["https://evil.example/landing"],
                "token_endpoint_auth_method": "none",
            },
        ).json()
        self.authenticate("o3p_mcp_plain", "o3p_mcp_plain")
        response = self.url_open(
            f"{AUTHORIZE_URL}?{urlencode({'client_id': registered['client_id']})}",
            allow_redirects=False,
        )
        self.assertErrorPage(response, status=403)
        self.assertIn("Return to evil.example", response.text)
        # The group is checked before the rest of the request.
        self.assertErrorPage(self.get_consent(response_type=None), status=403)

    def test_self_registered_client_errors_are_shown(self):
        self.client.registration_ip = "203.0.113.5"
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        for overrides, error in (
            ({"response_type": None}, "unsupported_response_type"),
            ({"code_challenge": None}, "invalid_request"),
            ({"code_challenge_method": "plain"}, "invalid_request"),
            ({"resource": "https://other.example.com/mcp"}, "invalid_target"),
            ({"scope": "okf.admin"}, "invalid_scope"),
        ):
            with self.subTest(error=error):
                response = self.get_consent(**overrides)
                self.assertErrorPage(response)
                self.assertIn("Return to client.example.com", response.text)
                self.assertIn(f"error={error}", response.text)
        # An explicit answer is still sent back to the client.
        self.assertClientError(self.decide("deny"), "access_denied")
        response = self.decide("approve")
        self.assertEqual(response.status_code, 303)
        self.assertIn("code", location_of(response)[1])

    def test_approve(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        response = self.decide("approve")
        self.assertEqual(response.status_code, 303)
        uri, query = location_of(response)
        self.assertEqual(uri, REDIRECT_URI)
        self.assertEqual(set(query), {"code", "state", "iss"})
        self.assertEqual(query["state"], "state-123")
        self.assertEqual(query["iss"], self.issuer)
        code = self.Code.search(
            [("code_hash", "=", hashlib.sha256(query["code"].encode()).hexdigest())]
        )
        self.assertEqual(code.client_id, self.client)
        self.assertEqual(code.user_id, self.editor)
        self.assertEqual(code.redirect_uri, REDIRECT_URI)
        self.assertEqual(code.code_challenge, self.challenge)
        self.assertEqual(code.scope, "okf.read okf.write")
        self.assertEqual(code.resource, RESOURCE)
        self.assertFalse(code.used)

    def test_reader_approval_grants_read_only(self):
        self.authenticate("o3p_mcp_reader", "o3p_mcp_reader")
        response = self.decide("approve", resource=None, state=None)
        _uri, query = location_of(response)
        self.assertNotIn("state", query)
        code = self.Code.search([("user_id", "=", self.reader.id)])
        self.assertEqual(code.scope, "okf.read")
        self.assertEqual(code.resource, RESOURCE)

    def test_deny(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        self.assertClientError(self.decide("deny"), "access_denied")
        self.assertFalse(self.Code.search([]))

    def test_decision_requires_csrf(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        response = self.decide("approve", csrf=False)
        self.assertEqual(response.status_code, 400)
        response = self.decide("approve", post_overrides={"csrf_token": "0" * 40 + "o1"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.Code.search([]))

    def test_decision_is_validated_again(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        response = self.decide(
            "approve",
            post_overrides={"redirect_uri": "https://attacker.example.com/callback"},
        )
        self.assertErrorPage(response)
        self.assertFalse(self.Code.search([]))

    def test_redirect_uri_query_and_private_scheme(self):
        uris = [
            "https://client.example.com/callback?tenant=7",
            "com.example.app:/oauth2redirect",
        ]
        self.client.redirect_uris = uris
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        for uri in uris:
            with self.subTest(uri=uri):
                response = self.decide("approve", redirect_uri=uri)
                self.assertEqual(response.status_code, 303)
                location, query = location_of(response)
                self.assertEqual(location, uri.split("?")[0])
                self.assertIn("code", query)
                if "tenant" in uri:
                    self.assertEqual(query["tenant"], "7")

    def test_options_request_cannot_approve(self):
        self.authenticate("o3p_mcp_editor", "o3p_mcp_editor")
        params = dict(self.authorization_params(), decision="approve")
        response = self.url_open(
            AUTHORIZE_URL, data=params, method="OPTIONS", allow_redirects=False
        )
        self.assertNotIn("code=", response.headers.get("Location", ""))
        self.assertFalse(self.Code.search([]))
