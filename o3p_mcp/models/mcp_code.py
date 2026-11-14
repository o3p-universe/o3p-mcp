from odoo import api, fields, models

from .oauth import (
    CODE_LIFETIME,
    SCOPE_READ,
    OAuthError,
    find_by_secret,
    hash_secret,
    new_secret,
    parse_scope,
    pkce_matches,
    same_resource,
    scope_string,
)


class O3pMcpCode(models.Model):
    _name = "o3p.mcp.code"
    _description = "MCP Authorization Code"
    _order = "id desc"

    code_hash = fields.Char(required=True, readonly=True, copy=False)
    client_id = fields.Many2one(
        "o3p.mcp.client",
        required=True,
        readonly=True,
        index=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        index=True,
        ondelete="cascade",
    )
    redirect_uri = fields.Char(string="Redirect URI", required=True, readonly=True)
    code_challenge = fields.Char(required=True, readonly=True)
    scope = fields.Char(required=True, readonly=True)
    resource = fields.Char(required=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    used = fields.Boolean(readonly=True)
    family = fields.Char(
        readonly=True,
        help="Token family issued in exchange for this code.",
    )

    _code_hash_unique = models.Constraint(
        "UNIQUE (code_hash)",
        "Authorization codes must be unique.",
    )

    @api.model
    def _create_code(self, client, user, redirect_uri, code_challenge, scopes, resource):
        """Store a new authorization code and return it in clear once."""
        code = new_secret()
        self.sudo().create(
            {
                "code_hash": hash_secret(code),
                "client_id": client.id,
                "user_id": user.id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "scope": scope_string(scopes),
                "resource": resource,
                "expires_at": fields.Datetime.now() + CODE_LIFETIME,
            }
        )
        return code

    @api.model
    def _find(self, code):
        return find_by_secret(self, "code_hash", code)

    def _claim(self):
        """Mark the code used; return False when it already was.

        The check and the update are one statement, so two concurrent
        exchanges of the same code cannot both succeed.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "UPDATE o3p_mcp_code SET used = TRUE"
            " WHERE id = %s AND used IS NOT TRUE RETURNING id",
            [self.id],
        )
        claimed = bool(self.env.cr.fetchone())
        self.invalidate_recordset(["used"])
        return claimed

    @api.model
    def _exchange(self, code, client, redirect_uri, code_verifier, resource=None):
        """Exchange an authorization code for a token; return the response.

        The code is consumed by the first exchange, successful or not. Using
        it again revokes the tokens it was exchanged for.
        """
        record = self._find(code)
        if not record or record.client_id != client:
            raise OAuthError("invalid_grant", "The authorization code is not valid.")
        if record.used or not record._claim():
            if record.family:
                self.env["o3p.mcp.token"].sudo().search(
                    [("family", "=", record.family)], limit=1
                )._revoke_family()
            raise OAuthError("invalid_grant", "The authorization code was already used.")
        if record.expires_at <= fields.Datetime.now():
            raise OAuthError("invalid_grant", "The authorization code has expired.")
        if redirect_uri:
            same_redirect = redirect_uri == record.redirect_uri
        else:
            # Allowed only where the authorization request could omit it too.
            same_redirect = client.redirect_uris == [record.redirect_uri]
        if not same_redirect:
            raise OAuthError(
                "invalid_grant", "The redirect URI does not match the authorization."
            )
        if not pkce_matches(code_verifier, record.code_challenge):
            raise OAuthError("invalid_grant", "The code verifier does not match.")
        if resource and not same_resource(resource, record.resource):
            raise OAuthError("invalid_target", "The resource does not match.")
        scopes = parse_scope(record.scope) & self.env["o3p.mcp.oauth"]._scopes_for_user(
            record.user_id
        )
        if SCOPE_READ not in scopes:
            raise OAuthError(
                "invalid_grant", "The user may no longer use the MCP server."
            )
        token, response = self.env["o3p.mcp.token"]._issue(
            client, record.user_id, scopes, record.resource
        )
        record.family = token.family
        return response
