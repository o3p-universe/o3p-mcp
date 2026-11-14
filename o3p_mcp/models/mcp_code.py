from odoo import api, fields, models

from .oauth import CODE_LIFETIME, find_by_secret, hash_secret, new_secret, scope_string


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
