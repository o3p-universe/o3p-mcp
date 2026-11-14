from odoo import api, models


SCOPE_READ = "okf.read"
SCOPE_WRITE = "okf.write"
# Each scope and the group that grants it, in the order scopes are listed.
SCOPE_GROUPS = {
    SCOPE_READ: "o3p_mcp.group_mcp_user",
    SCOPE_WRITE: "o3p_mcp.group_mcp_editor",
}
SCOPES = tuple(SCOPE_GROUPS)


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
