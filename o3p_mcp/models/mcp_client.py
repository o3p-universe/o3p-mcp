import secrets
from datetime import timedelta, timezone

from odoo import api, fields, models

from .oauth import (
    CLIENT_AUTH_METHODS,
    GRANT_TYPES,
    OAuthError,
    hash_secret,
    is_allowed_redirect_uri,
    new_secret,
    secret_matches,
)


AUTH_METHODS = list(
    zip(
        CLIENT_AUTH_METHODS,
        [
            "None (public client)",
            "Client secret in the request body",
            "Client secret with HTTP Basic",
        ],
        strict=True,
    )
)
REGISTRATION_LIMIT = 20
REGISTRATION_WINDOW = timedelta(hours=1)
MAX_REDIRECT_URIS = 10
MAX_CLIENT_NAME_LENGTH = 100
DEFAULT_CLIENT_NAME = "MCP client"
# Compared against when the client ID is unknown, so that unknown and known
# clients take the same time to reject.
_UNKNOWN_CLIENT_DIGEST = hash_secret("o3p_mcp unknown client")


def _new_client_id():
    return f"c_{secrets.token_hex(16)}"


class O3pMcpClient(models.Model):
    _name = "o3p.mcp.client"
    _description = "MCP OAuth Client"
    _order = "id desc"

    name = fields.Char(required=True)
    client_id = fields.Char(
        string="Client ID",
        required=True,
        readonly=True,
        copy=False,
        default=lambda self: _new_client_id(),
    )
    kind = fields.Selection(
        [("client", "MCP client"), ("resource_server", "Resource server")],
        required=True,
        default="client",
        readonly=True,
    )
    redirect_uris = fields.Json(string="Redirect URIs", default=lambda self: [])
    redirect_uris_text = fields.Text(
        string="Redirect URIs (one per line)",
        compute="_compute_redirect_uris_text",
    )
    token_endpoint_auth_method = fields.Selection(
        AUTH_METHODS,
        string="Authentication method",
        required=True,
        default="none",
    )
    secret_hash = fields.Char(copy=False, readonly=True, groups="base.group_system")
    registration_ip = fields.Char(string="Registration IP", readonly=True)
    active = fields.Boolean(default=True)
    last_used_at = fields.Datetime(readonly=True)
    token_ids = fields.One2many("o3p.mcp.token", "client_id", string="Tokens")

    _client_id_unique = models.Constraint(
        "UNIQUE (client_id)",
        "Each client ID can only be registered once.",
    )
    _registration_index = models.Index(
        "(registration_ip, create_date) WHERE registration_ip IS NOT NULL"
    )

    @api.depends("redirect_uris")
    def _compute_redirect_uris_text(self):
        for client in self:
            uris = client.redirect_uris
            client.redirect_uris_text = (
                "\n".join(uris) if isinstance(uris, list) else False
            )

    def _set_new_secret(self):
        """Store a new client secret and return it; it is never readable again."""
        self.ensure_one()
        secret = new_secret()
        self.sudo().secret_hash = hash_secret(secret)
        return secret

    @api.model
    def _authenticate(self, client_id, secret, method, kind="client"):
        """Return the active client of ``kind`` authenticated by ``method``.

        Raises ``invalid_client`` when the client is unknown, archived,
        registered with another method or the secret does not match.
        """
        client = self.sudo().browse()
        if isinstance(client_id, str) and client_id:
            client = client.search(
                [("client_id", "=", client_id), ("kind", "=", kind)], limit=1
            )
        if method == "none":
            authenticated = bool(client) and not secret
        else:
            digest = client.secret_hash if client else _UNKNOWN_CLIENT_DIGEST
            authenticated = secret_matches(secret, digest) and bool(client)
        if not authenticated or client.token_endpoint_auth_method != method:
            raise OAuthError(
                "invalid_client", "Client authentication failed.", status=401
            )
        return client

    @api.model
    def _register_client(self, metadata, ip):
        """Register a client from RFC 7591 ``metadata``; return the response.

        At most ``REGISTRATION_LIMIT`` clients are registered per address and
        hour. The client secret, for confidential clients, appears only in the
        returned response.
        """
        since = fields.Datetime.now() - REGISTRATION_WINDOW
        recent = self.sudo().with_context(active_test=False).search_count(
            [("registration_ip", "=", ip), ("create_date", ">=", since)]
        )
        if recent >= REGISTRATION_LIMIT:
            raise OAuthError(
                "temporarily_unavailable",
                "Too many client registrations from this address; try again later.",
                status=429,
            )
        if not isinstance(metadata, dict):
            raise OAuthError(
                "invalid_client_metadata", "The body must be a JSON object."
            )
        redirect_uris = metadata.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not 0 < len(redirect_uris) <= MAX_REDIRECT_URIS
            or not all(is_allowed_redirect_uri(uri) for uri in redirect_uris)
        ):
            raise OAuthError(
                "invalid_redirect_uri",
                "Redirect URIs must be https URLs, http URLs on a loopback host "
                "or private-use schemes, without a fragment.",
            )
        method = metadata.get("token_endpoint_auth_method", "client_secret_basic")
        grant_types = metadata.get("grant_types", ["authorization_code"])
        response_types = metadata.get("response_types", ["code"])
        if (
            method not in CLIENT_AUTH_METHODS
            or not isinstance(grant_types, list)
            or "authorization_code" not in grant_types
            or not all(grant in GRANT_TYPES for grant in grant_types)
            or response_types != ["code"]
        ):
            raise OAuthError(
                "invalid_client_metadata",
                "Supported: response type code, grant types authorization_code "
                "and refresh_token, authentication methods "
                + ", ".join(CLIENT_AUTH_METHODS)
                + ".",
            )
        name = metadata.get("client_name")
        name = name.strip()[:MAX_CLIENT_NAME_LENGTH] if isinstance(name, str) else ""
        client = self.sudo().create(
            {
                "name": name or DEFAULT_CLIENT_NAME,
                "redirect_uris": list(dict.fromkeys(redirect_uris)),
                "token_endpoint_auth_method": method,
                "registration_ip": ip,
            }
        )
        response = {
            "client_id": client.client_id,
            "client_id_issued_at": int(
                client.create_date.replace(tzinfo=timezone.utc).timestamp()
            ),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": list(dict.fromkeys(grant_types)),
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if method != "none":
            response["client_secret"] = client._set_new_secret()
            response["client_secret_expires_at"] = 0
        return response
