import hashlib
import json
import re

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.mcp_client import address_bucket
from ..models.oauth import OAuthError, display_target, is_allowed_redirect_uri, redirect_uri_matches


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
            "https://bücher.example/callback",
            "https://192.0.2.10/callback",
            "https://[2001:db8::1]:8443/callback",
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
            "https://evil.io\\.chatgpt.com/cb",
            "https://evil.io\\@app.example.com/cb",
            "https://app.example.com%2f.evil.io/cb",
            "https://exa_mple.com/cb",
            "https://-app.example.com/cb",
            "https://app.example.com./cb",
            "https://127.1/cb",
            "https://0x7f.0.0.1/cb",
            "https://@app.example.com/cb",
            "cursor://user@anysphere.cursor-retrieval/cb",
            "cursor://bad_host/cb",
            "/relative/callback",
            "",
            "https://app.example.com/" + "x" * 2000,
            None,
            42,
        ):
            with self.subTest(uri=uri):
                self.assertFalse(is_allowed_redirect_uri(uri))


    def test_loopback_redirect_uris_match_any_port(self):
        for registered, presented, matches in (
            ("http://127.0.0.1:33418/callback", "http://127.0.0.1:50123/callback", True),
            ("http://127.0.0.1/callback", "http://127.0.0.1:50123/callback", True),
            ("http://localhost:6274/cb?x=1", "http://localhost:1/cb?x=1", True),
            ("http://[::1]:8080/cb", "http://[::1]:9090/cb", True),
            ("http://127.0.0.1:33418/callback", "http://127.0.0.1:50123/other", False),
            ("http://127.0.0.1:33418/callback", "http://localhost:33418/callback", False),
            ("http://localhost:6274/cb?x=1", "http://localhost:6274/cb?x=2", False),
            ("http://127.0.0.1:33418/callback", "https://127.0.0.1:33418/callback", False),
            ("http://127.0.0.1:33418/callback", "http://127.0.0.1:50123/callback#x", False),
            ("http://127.0.0.1:33418/callback", "http://127.0.0.1:99999/callback", False),
            ("https://app.example.com/cb", "https://app.example.com:8443/cb", False),
            ("https://app.example.com/cb", "https://app.example.com/cb", True),
            ("https://app.example.com/cb", "https://APP.example.com/cb", False),
            ("com.example.app:/cb", "com.example.app:/cb", True),
            ("https://app.example.com/cb", None, False),
        ):
            with self.subTest(registered=registered, presented=presented):
                self.assertEqual(redirect_uri_matches(registered, presented), matches)

    def test_address_bucket(self):
        for ip, bucket in (
            ("203.0.113.7", "203.0.113.7"),
            ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
            ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
            ("::ffff:203.0.113.7", "203.0.113.7"),
            ("unknown", "unknown"),
        ):
            with self.subTest(ip=ip):
                self.assertEqual(address_bucket(ip), bucket)

    def test_display_target(self):
        for uri, shown in (
            ("https://bücher.example/cb", "xn--bcher-kva.example"),
            ("https://APP.example.com:8443/cb", "app.example.com:8443"),
            ("http://127.0.0.1:33418/cb", "127.0.0.1:33418"),
            ("http://[::1]:8080/cb", "[::1]:8080"),
            ("com.example.app:/oauth2redirect", "com.example.app:"),
            ("cursor://anysphere.cursor-retrieval/cb", "cursor://anysphere.cursor-retrieval"),
        ):
            with self.subTest(uri=uri):
                self.assertEqual(display_target(uri), shown)


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
            "o3p_mcp.issuer", "https://odoo.example.com/"
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
                "revocation_endpoint": f"{issuer}/o3p_mcp/oauth/revoke",
                "introspection_endpoint": f"{issuer}/o3p_mcp/oauth/introspect",
                "scopes_supported": ["okf.read", "okf.write", "odoo.read", "odoo.sql"],
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "revocation_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
                "authorization_response_iss_parameter_supported": True,
            },
        )

    def test_admin_login_does_not_move_the_issuer(self):
        # Review probe: Odoo rewrites web.base.url when an administrator signs
        # in on another host; the issuer must not follow it.
        params = self.env["ir.config_parameter"].sudo()
        params.set_str("web.base.url", "https://odoo.example.com")
        params.set_str("o3p_mcp.issuer", "https://odoo.example.com")
        before = self.url_open(METADATA_URL).json()
        page = self.url_open("/web/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        login = self.url_open(
            "/web/login",
            data={"login": "admin", "password": "admin", "csrf_token": csrf},
            allow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)
        self.assertNotEqual(params.get_str("web.base.url"), "https://odoo.example.com")
        after = self.url_open(METADATA_URL).json()
        self.assertEqual(after, before)
        self.assertEqual(after["issuer"], "https://odoo.example.com")

    def test_cors_preflight(self):
        # MCP clients in a browser send MCP-Protocol-Version with every
        # request, so the preflight must allow it next to the usual headers.
        for url, method in (
            (METADATA_URL, "GET"),
            (REGISTER_URL, "POST"),
            ("/o3p_mcp/oauth/token", "POST"),
            ("/o3p_mcp/oauth/revoke", "POST"),
        ):
            with self.subTest(url=url):
                response = self.url_open(
                    url,
                    method="OPTIONS",
                    headers={
                        "Origin": "http://localhost:6274",
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
                    },
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                self.assertEqual(
                    response.headers["Access-Control-Allow-Headers"],
                    "Authorization, Content-Type, MCP-Protocol-Version",
                )
                self.assertIn(method, response.headers["Access-Control-Allow-Methods"])
        # Server-to-server endpoints stay without CORS.
        for url in ("/o3p_mcp/oauth/introspect", "/o3p_mcp/api/v1/models"):
            with self.subTest(url=url):
                response = self.url_open(
                    url,
                    method="OPTIONS",
                    headers={
                        "Origin": "http://localhost:6274",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "mcp-protocol-version",
                    },
                )
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                self.assertNotIn("Access-Control-Allow-Headers", response.headers)

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
            # Review probe: a backslash makes the shown host differ from the real one.
            ({"redirect_uris": ["https://evil.io\\.chatgpt.com/cb"]}, "invalid_redirect_uri"),
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

    def test_rate_limit_counts_ipv6_per_64(self):
        Client = self.env["o3p.mcp.client"]
        payload = {
            "redirect_uris": ["http://127.0.0.1:9999/callback"],
            "token_endpoint_auth_method": "none",
        }
        for index in range(20):
            Client._register_client(payload, f"2001:db8:1:2::{index + 1:x}")
        with self.assertRaises(OAuthError) as caught:
            Client._register_client(payload, "2001:db8:1:2:ffff:ffff:ffff:ffff")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("client_id", Client._register_client(payload, "2001:db8:1:3::1"))
        client = Client.search([("registration_ip", "=", "2001:db8:1:3::1")])
        self.assertEqual(client.registration_bucket, "2001:db8:1:3::/64")
