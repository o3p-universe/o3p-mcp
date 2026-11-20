import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import idna

from odoo import api, models


SCOPE_READ = "okf.read"
SCOPE_WRITE = "okf.write"
SCOPE_ODOO_READ = "odoo.read"
# Permission-scoped SQL (the data API's sql operation).
SCOPE_ODOO_SQL = "odoo.sql"
# Each scope and the group that grants it, in the order scopes are listed.
SCOPE_GROUPS = {
    SCOPE_READ: "o3p_mcp.group_mcp_user",
    SCOPE_WRITE: "o3p_mcp.group_mcp_editor",
    SCOPE_ODOO_READ: "o3p_mcp.group_mcp_user",
    SCOPE_ODOO_SQL: "o3p_mcp.group_mcp_sql",
}
SCOPES = tuple(SCOPE_GROUPS)
# Asked for by clients that want refresh tokens; refresh tokens are always
# issued, so it is accepted and otherwise ignored.
SCOPE_OFFLINE = "offline_access"

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
DNS_LABEL_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
MAX_HOSTNAME_LENGTH = 253


def get_config(env, key, default=""):
    """Return the ``ir.config_parameter`` text value of ``key`` or ``default``."""
    value = env["ir.config_parameter"].sudo().get_param(key)
    return value if value else default


def get_config_int(env, key, default=0):
    """Return the integer value of ``key``, or ``default`` if unset or not a number."""
    try:
        return int(get_config(env, key, ""))
    except ValueError:
        return default


def set_config(env, key, value):
    """Store ``value`` under ``key``; an empty value removes the parameter."""
    value = "" if value is None or value is False else str(value)
    env["ir.config_parameter"].sudo().set_param(key, value or False)


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


def normalized_host(hostname):
    """Return the ASCII form of a DNS name or IP literal, or None.

    DNS names are converted with IDNA (UTS 46, as browsers do), so a
    look-alike Unicode name shows as its ``xn--`` form. Anything that is not
    a plain DNS name or IP address is refused, including a numeric last label
    (an IPv4 address in another notation, such as ``127.1``).
    """
    if not hostname:
        return None
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    try:
        ascii_host = idna.encode(hostname, uts46=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError):
        return None
    labels = ascii_host.split(".")
    if (
        len(ascii_host) > MAX_HOSTNAME_LENGTH
        or not all(DNS_LABEL_RE.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        return None
    return ascii_host


def is_allowed_redirect_uri(uri):
    """Tell whether ``uri`` may be registered as a redirect URI.

    Allowed: ``https`` URLs, ``http`` URLs on a loopback host and private-use
    schemes (RFC 8252). Fragments, user information, backslashes,
    whitespace, hosts that are not a DNS name or IP address, and the schemes
    a browser handles itself are refused.
    """
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > MAX_REDIRECT_URI_LENGTH
        or "#" in uri
        or "\\" in uri
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
    if parts.netloc and ("@" in parts.netloc or not normalized_host(hostname)):
        return False
    if scheme in ("http", "https"):
        if not parts.netloc:
            return False
        return scheme == "https" or hostname in LOOPBACK_HOSTS
    return bool(SCHEME_RE.fullmatch(scheme)) and scheme not in REJECTED_SCHEMES


def redirect_uri_matches(registered, presented):
    """Tell whether ``presented`` designates the registered redirect URI.

    The match is exact, except that an http redirect URI on a loopback host
    may use any port (RFC 8252 section 7.3): scheme, host, path and query
    must still be identical.
    """
    if not isinstance(presented, str) or not presented:
        return False
    if presented == registered:
        return True
    if not is_allowed_redirect_uri(presented):
        return False
    expected, actual = urlsplit(registered), urlsplit(presented)
    return (
        expected.scheme == actual.scheme == "http"
        and expected.hostname in LOOPBACK_HOSTS
        and expected.hostname == actual.hostname
        and expected.path == actual.path
        and expected.query == actual.query
    )


def is_valid_issuer(value):
    """Tell whether ``value`` may be the issuer: an https origin, or http on a
    loopback host, without path, query, fragment or user information."""
    if not isinstance(value, str) or "\\" in value:
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
        or not normalized_host(parts.hostname)
    ):
        return False
    return parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS
    )


def display_target(uri):
    """Return where ``uri`` leads, as shown to the user.

    ``https://bücher.example:8443/cb`` gives ``xn--bcher-kva.example:8443``;
    a private-use URI gives its scheme (and host, if any).
    """
    parts = urlsplit(uri)
    host = normalized_host(parts.hostname) if parts.netloc else None
    if host and ":" in host:
        host = f"[{host}]"
    if host and parts.port:
        host = f"{host}:{parts.port}"
    if parts.scheme in ("http", "https"):
        return host or ""
    return f"{parts.scheme}://{host}" if host else f"{parts.scheme}:"


def parse_scope(value):
    """Return the set of scopes in a space-separated scope string."""
    return set(value.split()) if isinstance(value, str) else set()


def known_scopes(value):
    """Return the scopes of ``value`` this server knows; the rest is dropped.

    ``offline_access`` and unknown scopes are ignored, as RFC 6749 section
    3.3 allows; the response's ``scope`` tells the client what it got.
    """
    return parse_scope(value) & set(SCOPES)


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
        scope at all. Only active internal users qualify: portal and public
        users (``share``) get nothing, whatever their groups.
        """
        user = user.sudo()
        if not user or not user.active or user.share:
            return set()
        scopes = {
            scope for scope, group in SCOPE_GROUPS.items() if user.has_group(group)
        }
        return scopes if SCOPE_READ in scopes else set()

    @api.model
    def _resource_url(self):
        """Return the public MCP URL: the only resource and token audience."""
        return get_config(self.env, "o3p_mcp.resource_url").strip()

    @api.model
    def _scope_descriptions(self, scopes):
        descriptions = {
            SCOPE_READ: self.env._("Search and read the knowledge base"),
            SCOPE_WRITE: self.env._(
                "Change the knowledge base with write tools and producer runs"
            ),
            SCOPE_ODOO_READ: self.env._(
                "Read the Odoo records and fields your account can see, without "
                "changing anything"
            ),
            SCOPE_ODOO_SQL: self.env._(
                "Run read-only SQL limited to what your account can see"
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
        elif not any(redirect_uri_matches(uri, redirect_uri) for uri in registered):
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
        # Without a known scope, everything is requested; okf.read is needed
        # for any call, so it is always part of the grant.
        requested = (known_scopes(params.get("scope")) or set(SCOPES)) | {SCOPE_READ}
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
        """Return the issuer, without a trailing slash.

        It is the ``o3p_mcp.issuer`` setting, not ``web.base.url``: Odoo
        rewrites ``web.base.url`` whenever an administrator signs in, which
        would change the issuer, the endpoints and ``iss`` under the clients.
        """
        issuer = get_config(self.env, "o3p_mcp.issuer") or get_config(
            self.env, "web.base.url"
        )
        return issuer.strip().rstrip("/")

    @api.model
    def _init_issuer(self):
        """Initialise the issuer from ``web.base.url`` unless already set."""
        if not get_config(self.env, "o3p_mcp.issuer"):
            base_url = get_config(self.env, "web.base.url")
            set_config(self.env, "o3p_mcp.issuer", base_url.strip().rstrip("/"))

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
            "scopes_supported": [*SCOPES, SCOPE_OFFLINE],
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
        minutes = get_config_int(
            self.env, "o3p_mcp.access_token_minutes", DEFAULT_ACCESS_TOKEN_MINUTES
        )
        return timedelta(minutes=max(minutes, 1))

    @api.model
    def _refresh_token_lifetime(self):
        days = get_config_int(
            self.env, "o3p_mcp.refresh_token_days", DEFAULT_REFRESH_TOKEN_DAYS
        )
        return timedelta(days=max(days, 1))
