import secrets

from odoo import api, fields, models

from .oauth import OAuthError, hash_secret, new_secret, secret_matches


AUTH_METHODS = [
    ("none", "None (public client)"),
    ("client_secret_post", "Client secret in the request body"),
    ("client_secret_basic", "Client secret with HTTP Basic"),
]
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
