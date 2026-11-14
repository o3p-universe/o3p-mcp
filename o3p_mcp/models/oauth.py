import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import api, models


SCOPE_READ = "okf.read"
SCOPE_WRITE = "okf.write"
# Each scope and the group that grants it, in the order scopes are listed.
SCOPE_GROUPS = {
    SCOPE_READ: "o3p_mcp.group_mcp_user",
    SCOPE_WRITE: "o3p_mcp.group_mcp_editor",
}
SCOPES = tuple(SCOPE_GROUPS)

CLIENT_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
GRANT_TYPES = ("authorization_code", "refresh_token")

METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/o3p_mcp/oauth/authorize"
TOKEN_PATH = "/o3p_mcp/oauth/token"
REGISTER_PATH = "/o3p_mcp/oauth/register"

CODE_LIFETIME = timedelta(minutes=5)
DEFAULT_ACCESS_TOKEN_MINUTES = 60
DEFAULT_REFRESH_TOKEN_DAYS = 30
# Longer values are never ours: they are rejected before hashing.
MAX_SECRET_LENGTH = 512
MAX_REDIRECT_URI_LENGTH = 2000

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Schemes a browser would handle itself instead of handing to an application.
REJECTED_SCHEMES = frozenset(
    {
        "about", "blob", "chrome", "data", "file", "filesystem", "ftp",
        "javascript", "jar", "mailto", "sms", "tel", "vbscript", "view-source",
        "ws", "wss",
    }
)
SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*")


def new_secret():
    """Return a new random 256-bit secret as URL-safe text."""
    return secrets.token_urlsafe(32)


def hash_secret(value):
    """Return the hex SHA-256 digest under which ``value`` is stored."""
    return hashlib.sha256(value.encode()).hexdigest()


def secret_matches(value, digest):
    """Compare ``value`` with a stored digest in constant time."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SECRET_LENGTH
        or not digest
    ):
        return False
    return hmac.compare_digest(hash_secret(value), digest)


def find_by_secret(model, field, value):
    """Return the record whose ``field`` holds the digest of ``value``.

    The lookup ignores ``active`` and runs as superuser; callers decide what
    the record may be used for.
    """
    empty = model.sudo().browse()
    if not isinstance(value, str) or not value or len(value) > MAX_SECRET_LENGTH:
        return empty
    digest = hash_secret(value)
    record = empty.with_context(active_test=False).search(
        [(field, "=", digest)], limit=1
    )
    if record and hmac.compare_digest(record[field], digest):
        return record
    return empty


def is_allowed_redirect_uri(uri):
    """Tell whether ``uri`` may be registered as a redirect URI.

    Allowed: ``https`` URLs, ``http`` URLs on a loopback host and private-use
    schemes (RFC 8252). Fragments, user information, whitespace and the
    schemes a browser handles itself are refused.
    """
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > MAX_REDIRECT_URI_LENGTH
        or "#" in uri
        or any(char.isspace() or not char.isprintable() for char in uri)
    ):
        return False
    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if not hostname or parts.username is not None or parts.password is not None:
            return False
        return scheme == "https" or hostname in LOOPBACK_HOSTS
    return bool(SCHEME_RE.fullmatch(scheme)) and scheme not in REJECTED_SCHEMES


def parse_scope(value):
    """Return the set of scopes in a space-separated scope string."""
    return set(value.split()) if isinstance(value, str) else set()


def scope_string(scopes):
    """Return ``scopes`` as a space-separated string in canonical order."""
    return " ".join(scope for scope in SCOPES if scope in scopes)


class OAuthError(Exception):
    """An OAuth error answered to the client.

    ``description`` is shown to the client: it never contains a token, a code
    or a secret.
    """

    def __init__(self, error, description=None, status=400):
        super().__init__(error)
        self.error = error
        self.description = description
        self.status = status

    def to_dict(self):
        payload = {"error": self.error}
        if self.description:
            payload["error_description"] = self.description
        return payload


class O3pMcpOauth(models.AbstractModel):
    _name = "o3p.mcp.oauth"
    _description = "MCP OAuth Service"

    @api.model
    def _scopes_for_user(self, user):
        """Return the scopes ``user`` may hold now, from its state and groups.

        ``okf.read`` is required for every call, so a user without it gets no
        scope at all.
        """
        user = user.sudo()
        if not user or not user.active or user._is_public():
            return set()
        scopes = {
            scope for scope, group in SCOPE_GROUPS.items() if user.has_group(group)
        }
        return scopes if SCOPE_READ in scopes else set()

    @api.model
    def _issuer(self):
        """Return the issuer: ``web.base.url`` without a trailing slash."""
        base_url = self.env["ir.config_parameter"].sudo().get_str("web.base.url")
        return (base_url or "").rstrip("/")

    @api.model
    def _metadata(self):
        """Return the RFC 8414 authorization server metadata."""
        issuer = self._issuer()
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}{AUTHORIZE_PATH}",
            "token_endpoint": f"{issuer}{TOKEN_PATH}",
            "registration_endpoint": f"{issuer}{REGISTER_PATH}",
            "scopes_supported": list(SCOPES),
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": list(GRANT_TYPES),
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(CLIENT_AUTH_METHODS),
            "authorization_response_iss_parameter_supported": True,
        }

    @api.model
    def _access_token_lifetime(self):
        minutes = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_int("o3p_mcp.access_token_minutes", DEFAULT_ACCESS_TOKEN_MINUTES)
        )
        return timedelta(minutes=max(minutes, 1))

    @api.model
    def _refresh_token_lifetime(self):
        days = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_int("o3p_mcp.refresh_token_days", DEFAULT_REFRESH_TOKEN_DAYS)
        )
        return timedelta(days=max(days, 1))
