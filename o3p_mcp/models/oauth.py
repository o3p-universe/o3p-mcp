import hashlib
import hmac
import secrets
from datetime import timedelta

from odoo import api, models


SCOPE_READ = "okf.read"
SCOPE_WRITE = "okf.write"
# Each scope and the group that grants it, in the order scopes are listed.
SCOPE_GROUPS = {
    SCOPE_READ: "o3p_mcp.group_mcp_user",
    SCOPE_WRITE: "o3p_mcp.group_mcp_editor",
}
SCOPES = tuple(SCOPE_GROUPS)

CODE_LIFETIME = timedelta(minutes=5)
DEFAULT_ACCESS_TOKEN_MINUTES = 60
DEFAULT_REFRESH_TOKEN_DAYS = 30
# Longer values are never ours: they are rejected before hashing.
MAX_SECRET_LENGTH = 512


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
