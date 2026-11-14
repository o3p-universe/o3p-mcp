import secrets
from datetime import timedelta, timezone

from odoo import api, fields, models

from .oauth import (
    SCOPE_READ,
    OAuthError,
    find_by_secret,
    hash_secret,
    new_secret,
    parse_scope,
    same_resource,
    scope_string,
)


PURGE_BATCH_SIZE = 1000
# last_used_at is written at most this often per token.
LAST_USED_RESOLUTION = timedelta(minutes=1)
INACTIVE = {"active": False}


def _epoch(value):
    return int(value.replace(tzinfo=timezone.utc).timestamp())


class O3pMcpToken(models.Model):
    _name = "o3p.mcp.token"
    _description = "MCP Access Token"
    _order = "id desc"

    access_hash = fields.Char(required=True, readonly=True, copy=False)
    refresh_hash = fields.Char(readonly=True, copy=False)
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
    scope = fields.Char(readonly=True)
    resource = fields.Char(readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True)
    refresh_expires_at = fields.Datetime(readonly=True)
    family = fields.Char(
        required=True,
        readonly=True,
        index=True,
        help="Tokens issued from one authorization and its refresh rotations.",
    )
    rotated_at = fields.Datetime(
        readonly=True,
        help="When the refresh token was exchanged for the next token.",
    )
    revoked_at = fields.Datetime(readonly=True)
    last_used_at = fields.Datetime(readonly=True)
    state = fields.Selection(
        [
            ("active", "Active"),
            ("rotated", "Rotated"),
            ("expired", "Expired"),
            ("revoked", "Revoked"),
        ],
        compute="_compute_state",
    )

    _access_hash_unique = models.Constraint(
        "UNIQUE (access_hash)",
        "Access tokens must be unique.",
    )
    _refresh_hash_unique = models.Constraint(
        "UNIQUE (refresh_hash)",
        "Refresh tokens must be unique.",
    )

    @api.depends("expires_at", "refresh_expires_at", "rotated_at", "revoked_at")
    def _compute_state(self):
        now = fields.Datetime.now()
        for token in self:
            ends = token.refresh_expires_at or token.expires_at
            if token.expires_at and ends < token.expires_at:
                ends = token.expires_at
            if token.revoked_at:
                token.state = "revoked"
            elif token.rotated_at:
                token.state = "rotated"
            elif not ends or ends <= now:
                token.state = "expired"
            else:
                token.state = "active"

    @api.depends("client_id.name", "user_id.name")
    def _compute_display_name(self):
        for token in self:
            token.display_name = f"{token.client_id.name} / {token.user_id.name}"

    # ------------------------------------------------------------------
    # Issuing, rotation and revocation
    # ------------------------------------------------------------------

    @api.model
    def _issue(self, client, user, scopes, resource, family=None):
        """Create a token and return ``(token, response)``.

        ``response`` is the RFC 6749 token response and the only place where
        the access and refresh tokens appear in clear.
        """
        oauth = self.env["o3p.mcp.oauth"]
        now = fields.Datetime.now()
        access_lifetime = oauth._access_token_lifetime()
        access_token, refresh_token = new_secret(), new_secret()
        token = self.sudo().create(
            {
                "access_hash": hash_secret(access_token),
                "refresh_hash": hash_secret(refresh_token),
                "client_id": client.id,
                "user_id": user.id,
                "scope": scope_string(scopes),
                "resource": resource,
                "expires_at": now + access_lifetime,
                "refresh_expires_at": now + oauth._refresh_token_lifetime(),
                "family": family or secrets.token_hex(16),
            }
        )
        client.sudo().last_used_at = now
        return token, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(access_lifetime.total_seconds()),
            "refresh_token": refresh_token,
            "scope": token.scope,
        }

    @api.model
    def _rotate(self, refresh_token, client, scopes=None, resource=None):
        """Exchange a refresh token for a new token of the same family.

        A refresh token works once. Presenting one that was already rotated
        revokes the whole family. ``scopes`` may narrow the grant; the user's
        current groups narrow it further.
        """
        token = find_by_secret(self, "refresh_hash", refresh_token)
        if not token or token.client_id != client:
            raise OAuthError("invalid_grant", "The refresh token is not valid.")
        if token.revoked_at or token.rotated_at:
            token._revoke_family()
            raise OAuthError("invalid_grant", "The refresh token is not valid.")
        if token.refresh_expires_at <= fields.Datetime.now():
            raise OAuthError("invalid_grant", "The refresh token has expired.")
        granted = parse_scope(token.scope)
        requested = granted if scopes is None else set(scopes)
        if not requested or not requested <= granted:
            raise OAuthError(
                "invalid_scope", "The scope exceeds the original grant."
            )
        if resource and not same_resource(resource, token.resource):
            raise OAuthError("invalid_target", "The resource does not match.")
        if not token._claim_refresh():
            token._revoke_family()
            raise OAuthError("invalid_grant", "The refresh token is not valid.")
        allowed = requested & self.env["o3p.mcp.oauth"]._scopes_for_user(
            token.user_id
        )
        if SCOPE_READ not in allowed:
            token._revoke_family()
            raise OAuthError(
                "invalid_grant", "The user may no longer use the MCP server."
            )
        return self._issue(
            client, token.user_id, allowed, token.resource, family=token.family
        )

    def _claim_refresh(self):
        """Mark the refresh token used; return False when it already was."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "UPDATE o3p_mcp_token SET rotated_at = %s"
            " WHERE id = %s AND rotated_at IS NULL AND revoked_at IS NULL"
            " RETURNING id",
            [fields.Datetime.now(), self.id],
        )
        claimed = bool(self.env.cr.fetchone())
        self.invalidate_recordset(["rotated_at"])
        return claimed

    def _revoke_family(self):
        """Revoke every token issued from the same authorizations as ``self``."""
        families = [family for family in set(self.mapped("family")) if family]
        if not families:
            return
        self.sudo().search(
            [("family", "in", families), ("revoked_at", "=", False)]
        ).write({"revoked_at": fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @api.model
    def _introspect(self, access_token):
        """Return the introspection answer for ``access_token``.

        Scopes are the granted scopes still allowed by the user's current
        groups; a token left without ``okf.read`` is inactive.
        """
        token = find_by_secret(self, "access_hash", access_token)
        now = fields.Datetime.now()
        if (
            not token
            or token.revoked_at
            or token.expires_at <= now
            or not token.client_id.active
        ):
            return dict(INACTIVE)
        user = token.user_id
        scopes = parse_scope(token.scope) & self.env[
            "o3p.mcp.oauth"
        ]._scopes_for_user(user)
        if SCOPE_READ not in scopes:
            return dict(INACTIVE)
        if not token.last_used_at or now - token.last_used_at >= LAST_USED_RESOLUTION:
            token.last_used_at = now
        answer = {
            "active": True,
            "sub": str(user.id),
            "username": user.login,
            "name": user.name,
            "email": user.email,
            "scope": scope_string(scopes),
            "aud": token.resource,
            "exp": _epoch(token.expires_at),
            "client_id": token.client_id.client_id,
        }
        if not answer["email"]:
            del answer["email"]
        return answer

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    @api.model
    def _purge_expired(self, limit=PURGE_BATCH_SIZE):
        """Delete up to ``limit`` expired codes and tokens; return the count.

        A token stays until its refresh token has expired, so a replayed
        refresh token is recognised for as long as it could have been used.
        """
        now = fields.Datetime.now()
        codes = self.env["o3p.mcp.code"].sudo().search(
            [("expires_at", "<", now)], limit=limit
        )
        tokens = self.sudo().browse()
        if len(codes) < limit:
            tokens = tokens.search(
                [
                    "|",
                    ("refresh_expires_at", "<", now),
                    "&",
                    ("refresh_expires_at", "=", False),
                    ("expires_at", "<", now),
                ],
                limit=limit - len(codes),
            )
        count = len(codes) + len(tokens)
        codes.unlink()
        tokens.unlink()
        return count

    @api.model
    def _cron_purge(self):
        cron = self.env["ir.cron"]
        while True:
            purged = self._purge_expired()
            if purged < PURGE_BATCH_SIZE:
                cron._commit_progress(purged, remaining=0)
                return
            if not cron._commit_progress(purged):
                return
