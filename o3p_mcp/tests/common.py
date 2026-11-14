import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

from odoo import Command
from odoo.tests import HttpCase, new_test_user


RESOURCE = "https://mcp.example.com/mcp"
REDIRECT_URI = "https://client.example.com/callback"
AUTHORIZE_URL = "/o3p_mcp/oauth/authorize"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def location_of(response):
    """Return ``(uri without query, query dict)`` of a redirect response."""
    parts = urlsplit(response.headers["Location"])
    return parts._replace(query="").geturl(), dict(parse_qsl(parts.query))


class O3pMcpCommon:
    """Users, groups and a public client shared by the o3p_mcp tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_group = cls.env.ref("o3p_mcp.group_mcp_user")
        cls.editor_group = cls.env.ref("o3p_mcp.group_mcp_editor")
        cls.plain = new_test_user(
            cls.env, "o3p_mcp_plain", groups="base.group_user", name="Plain User"
        )
        cls.reader = new_test_user(
            cls.env,
            "o3p_mcp_reader",
            groups="base.group_user,o3p_mcp.group_mcp_user",
            name="MCP Reader",
        )
        cls.editor = new_test_user(
            cls.env,
            "o3p_mcp_editor",
            groups="base.group_user,o3p_mcp.group_mcp_user,o3p_mcp.group_mcp_editor",
            name="MCP Editor",
        )
        cls.client = cls.env["o3p.mcp.client"].create(
            {
                "name": "Test MCP client",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
            }
        )
        cls.Token = cls.env["o3p.mcp.token"]
        cls.Code = cls.env["o3p.mcp.code"]

    @classmethod
    def demote_to_reader(cls, user):
        user.write(
            {
                "group_ids": [
                    Command.unlink(cls.editor_group.id),
                    Command.link(cls.user_group.id),
                ]
            }
        )

    @classmethod
    def remove_mcp_access(cls, user):
        user.write(
            {
                "group_ids": [
                    Command.unlink(cls.editor_group.id),
                    Command.unlink(cls.user_group.id),
                ]
            }
        )


class O3pMcpHttpCase(O3pMcpCommon, HttpCase):
    """Drives the authorization endpoint as a signed-in browser."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_str("o3p_mcp.resource_url", RESOURCE)
        cls.issuer = cls.env["o3p.mcp.oauth"]._issuer()

    def setUp(self):
        super().setUp()
        self.verifier, self.challenge = pkce_pair()

    def authorization_params(self, **overrides):
        params = {
            "response_type": "code",
            "client_id": self.client.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "okf.read okf.write",
            "state": "state-123",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }
        params.update(overrides)
        return {key: value for key, value in params.items() if value is not None}

    def get_consent(self, **overrides):
        query = urlencode(self.authorization_params(**overrides))
        return self.url_open(f"{AUTHORIZE_URL}?{query}", allow_redirects=False)

    def decide(self, decision="approve", csrf=True, post_overrides=None, **overrides):
        page = self.get_consent(**overrides)
        self.assertEqual(page.status_code, 200, page.text[:500])
        data = dict(self.authorization_params(**overrides), decision=decision)
        if csrf:
            data["csrf_token"] = CSRF_RE.search(page.text).group(1)
        data.update(post_overrides or {})
        return self.url_open(AUTHORIZE_URL, data=data, allow_redirects=False)

    def authorize_code(self, login="o3p_mcp_editor", **overrides):
        """Sign in as ``login``, approve and return the authorization code."""
        self.authenticate(login, login)
        response = self.decide(**overrides)
        self.assertEqual(response.status_code, 303, response.text[:500])
        _uri, query = location_of(response)
        return query["code"]
