import base64
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
INTROSPECT_PATH = "/o3p_mcp/oauth/introspect"
REVOKE_PATH = "/o3p_mcp/oauth/revoke"

# An S256 code challenge is a base64url SHA-256 digest without padding.
CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
CODE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")

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


def pkce_matches(verifier, challenge):
    """Check an RFC 7636 ``S256`` code verifier against its challenge."""
    if not isinstance(verifier, str) or not CODE_VERIFIER_RE.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge or "")


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


class AuthorizationError(OAuthError):
    """An authorization request error.

    Without ``redirect_uri`` the client or its redirect URI could not be
    trusted: the error is shown to the user and never redirected. With
    ``show`` the error is shown too, with a link back to the client.
    """

    def __init__(self, error, description, redirect_uri=None, state=None, show=False):
        super().__init__(error, description)
        self.redirect_uri = redirect_uri
        self.state = state
        self.show = show or not redirect_uri


def same_resource(left, right):
    """Compare two resource URLs, ignoring a trailing slash."""
    return left.rstrip("/") == right.rstrip("/")


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
    def _resource_url(self):
        """Return the public MCP URL: the only resource and token audience."""
        return (
            self.env["ir.config_parameter"].sudo().get_str("o3p_mcp.resource_url") or ""
        ).strip()

    @api.model
    def _scope_descriptions(self, scopes):
        descriptions = {
            SCOPE_READ: self.env._("Search and read the knowledge base"),
            SCOPE_WRITE: self.env._(
                "Change the knowledge base with write tools and producer runs"
            ),
        }
        return [(scope, descriptions[scope]) for scope in SCOPES if scope in scopes]

    @api.model
    def _check_authorization(self, params, user):
        """Validate an authorization request made by ``user``.

        Returns the client, the redirect URI, the state, the code challenge,
        the scopes to grant and the resource. Raises ``AuthorizationError``.
        """
        _ = self.env._
        client_id = params.get("client_id")
        client = self.env["o3p.mcp.client"].sudo().browse()
        if isinstance(client_id, str) and client_id:
            client = client.search(
                [("client_id", "=", client_id), ("kind", "=", "client")], limit=1
            )
        if not client:
            raise AuthorizationError(
                "invalid_request", _("This application is unknown or disabled.")
            )
        registered = client.redirect_uris if isinstance(client.redirect_uris, list) else []
        redirect_uri = params.get("redirect_uri")
        if not redirect_uri and len(registered) == 1:
            redirect_uri = registered[0]
        elif redirect_uri not in registered:
            raise AuthorizationError(
                "invalid_request",
                _("The redirect URI is not registered for this application."),
            )
        state = params.get("state") or None
        # A user without MCP access never causes a redirect to the client.
        allowed = self._scopes_for_user(user)
        if not allowed:
            raise AuthorizationError(
                "access_denied",
                _("Your account is not allowed to use the MCP server."),
                redirect_uri,
                state,
                show=True,
            )
        # Anyone can register a client, so a self-registered client's redirect
        # URI is only followed once the user has approved or denied.
        self_registered = bool(client.registration_ip)

        def fail(error, description):
            raise AuthorizationError(
                error, description, redirect_uri, state, show=self_registered
            )

        if params.get("response_type") != "code":
            fail("unsupported_response_type", _("Only response_type=code is supported."))
        challenge = params.get("code_challenge")
        if (
            params.get("code_challenge_method") != "S256"
            or not isinstance(challenge, str)
            or not CODE_CHALLENGE_RE.fullmatch(challenge)
        ):
            fail(
                "invalid_request",
                _("PKCE is required: send code_challenge with code_challenge_method=S256."),
            )
        resource_url = self._resource_url()
        if not resource_url:
            raise AuthorizationError(
                "server_error",
                _("The MCP server URL is not configured in Odoo."),
                redirect_uri,
                state,
                show=True,
            )
        resource = params.get("resource")
        if resource and not (
            isinstance(resource, str) and same_resource(resource, resource_url)
        ):
            fail("invalid_target", _("Tokens are only issued for %s.", resource_url))
        requested = parse_scope(params.get("scope")) or set(SCOPES)
        if not requested <= set(SCOPES) or SCOPE_READ not in requested:
            fail(
                "invalid_scope",
                _("Supported scopes are %s; okf.read is required.", " ".join(SCOPES)),
            )
        return {
            "client": client,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "scopes": requested & allowed,
            "resource": resource_url,
        }

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
            "revocation_endpoint": f"{issuer}{REVOKE_PATH}",
            "introspection_endpoint": f"{issuer}{INTROSPECT_PATH}",
            "scopes_supported": list(SCOPES),
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": list(GRANT_TYPES),
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": list(CLIENT_AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(CLIENT_AUTH_METHODS),
            "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
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
