import hashlib
import json

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.oauth import is_allowed_redirect_uri


METADATA_URL = "/.well-known/oauth-authorization-server"
REGISTER_URL = "/o3p_mcp/oauth/register"


@tagged("post_install", "-at_install")
class TestRedirectUriPolicy(TransactionCase):
    def test_allowed(self):
        for uri in (
            "https://app.example.com/callback",
            "https://app.example.com/callback?tenant=1",
            "http://127.0.0.1:33418/",
            "http://localhost:6274/oauth/callback",
            "http://[::1]:8080/cb",
            "com.example.app:/oauth2redirect",
            "cursor://anysphere.cursor-retrieval/oauth/callback",
        ):
            with self.subTest(uri=uri):
                self.assertTrue(is_allowed_redirect_uri(uri))

    def test_refused(self):
        for uri in (
            "http://app.example.com/callback",
            "http://localhost.example.com/callback",
            "https://app.example.com/callback#fragment",
            "https://user:password@app.example.com/callback",
            "https:///no-host",
            "https://app.example.com/a b",
            "https://app.example.com:99999/",
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html,hello",
            "file:///etc/passwd",
            "ws://localhost/callback",
            "/relative/callback",
            "",
            "https://app.example.com/" + "x" * 2000,
            None,
            42,
        ):
            with self.subTest(uri=uri):
                self.assertFalse(is_allowed_redirect_uri(uri))


@tagged("post_install", "-at_install")
class TestMetadataAndRegistration(HttpCase):
    def register(self, payload=None, raw=None):
        return self.url_open(
            REGISTER_URL,
            data=raw if raw is not None else json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    def test_metadata(self):
        self.env["ir.config_parameter"].sudo().set_str(
            "web.base.url", "https://odoo.example.com/"
        )
        response = self.url_open(METADATA_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertNotIn("session_id", response.cookies)
        issuer = "https://odoo.example.com"
        self.assertEqual(
            response.json(),
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/o3p_mcp/oauth/authorize",
                "token_endpoint": f"{issuer}/o3p_mcp/oauth/token",
                "registration_endpoint": f"{issuer}/o3p_mcp/oauth/register",
                "scopes_supported": ["okf.read", "okf.write"],
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "authorization_response_iss_parameter_supported": True,
            },
        )

    def test_cors_preflight(self):
        for url in (METADATA_URL, REGISTER_URL):
            with self.subTest(url=url):
                response = self.url_open(
                    url,
                    method="OPTIONS",
                    headers={
                        "Origin": "https://inspector.example.com",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                self.assertIn(
                    "Content-Type", response.headers["Access-Control-Allow-Headers"]
                )

    def test_register_public_client(self):
        response = self.register(
            {
                "client_name": "  Inspector  ",
                "redirect_uris": ["http://localhost:6274/oauth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "okf.read okf.write",
            }
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        data = response.json()
        client = self.env["o3p.mcp.client"].search(
            [("client_id", "=", data["client_id"])]
        )
        self.assertTrue(client)
        self.assertEqual(
            data,
            {
                "client_id": client.client_id,
                "client_id_issued_at": data["client_id_issued_at"],
                "client_name": "Inspector",
                "redirect_uris": ["http://localhost:6274/oauth/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        self.assertIsInstance(data["client_id_issued_at"], int)
        self.assertEqual(client.kind, "client")
        self.assertEqual(client.registration_ip, "127.0.0.1")
        self.assertFalse(client.secret_hash)

    def test_register_confidential_client(self):
        response = self.register(
            {"redirect_uris": ["https://app.example.com/callback"]}
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["token_endpoint_auth_method"], "client_secret_basic")
        self.assertEqual(data["client_name"], "MCP client")
        self.assertEqual(data["client_secret_expires_at"], 0)
        self.assertGreaterEqual(len(data["client_secret"]), 43)
        client = self.env["o3p.mcp.client"].search(
            [("client_id", "=", data["client_id"])]
        )
        self.assertEqual(
            client.secret_hash,
            hashlib.sha256(data["client_secret"].encode()).hexdigest(),
        )

    def test_invalid_registrations(self):
        cases = [
            ({"client_name": "No URIs"}, "invalid_redirect_uri"),
            ({"redirect_uris": []}, "invalid_redirect_uri"),
            ({"redirect_uris": "https://app.example.com/cb"}, "invalid_redirect_uri"),
            ({"redirect_uris": ["http://app.example.com/cb"]}, "invalid_redirect_uri"),
            ({"redirect_uris": ["javascript:alert(1)"]}, "invalid_redirect_uri"),
            (
                {
                    "redirect_uris": ["https://app.example.com/cb"],
                    "token_endpoint_auth_method": "private_key_jwt",
                },
                "invalid_client_metadata",
            ),
            (
                {
                    "redirect_uris": ["https://app.example.com/cb"],
                    "grant_types": ["client_credentials"],
                },
                "invalid_client_metadata",
            ),
            (
                {
                    "redirect_uris": ["https://app.example.com/cb"],
                    "response_types": ["token"],
                },
                "invalid_client_metadata",
            ),
            (["not", "an", "object"], "invalid_client_metadata"),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload):
                response = self.register(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], error)
        response = self.register(raw="{not json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        self.assertFalse(
            self.env["o3p.mcp.client"].search([("registration_ip", "!=", False)])
        )

    def test_rate_limit(self):
        Client = self.env["o3p.mcp.client"]
        payload = {
            "redirect_uris": ["http://127.0.0.1:9999/callback"],
            "token_endpoint_auth_method": "none",
        }
        for _index in range(19):
            Client._register_client(payload, "127.0.0.1")
        self.assertEqual(self.register(payload).status_code, 201)
        response = self.register(payload)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "3600")
        self.assertEqual(response.json()["error"], "temporarily_unavailable")
        # Other addresses are counted separately.
        self.assertIn("client_id", Client._register_client(payload, "203.0.113.7"))
