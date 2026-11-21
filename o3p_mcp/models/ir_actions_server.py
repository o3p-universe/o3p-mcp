import re

from odoo import fields, models


class IrActionsServer(models.Model):
    _inherit = "ir.actions.server"

    # The same switch as on Odoo's own MCP server: a server action is made
    # available on its form. Here it stands for an exposed action
    # (o3p.mcp.action) of this server action, which holds the rest.
    o3p_mcp_available = fields.Boolean(
        string="Available in MCP",
        compute="_compute_o3p_mcp_action_id",
        inverse="_inverse_o3p_mcp_available",
        groups="base.group_system",
        help="Users of MCP records / Editor may run this server action through "
        "MCP, on records they may change in Odoo. Its name for MCP, its "
        "description, the groups it is limited to and the number of records "
        "per call are set on the exposed action.",
    )
    o3p_mcp_action_id = fields.Many2one(
        "o3p.mcp.action",
        string="Exposed action",
        compute="_compute_o3p_mcp_action_id",
        groups="base.group_system",
    )

    def _o3p_mcp_actions(self):
        """Return the exposed actions of these server actions, archived ones too."""
        return (
            self.env["o3p.mcp.action"]
            .with_context(active_test=False)
            .search([("kind", "=", "server"), ("server_action_id", "in", self.ids)], order="id")
        )

    def _compute_o3p_mcp_action_id(self):
        exposed = self._o3p_mcp_actions().filtered("active")
        for server_action in self:
            action = exposed.filtered(lambda item: item.server_action_id == server_action)[:1]
            server_action.o3p_mcp_action_id = action
            server_action.o3p_mcp_available = bool(action)

    def _inverse_o3p_mcp_available(self):
        known = self._o3p_mcp_actions()
        for server_action in self:
            mine = known.filtered(lambda item: item.server_action_id == server_action)
            if not server_action.o3p_mcp_available:
                mine.filtered("active").active = False
            elif not mine.filtered("active"):
                if mine:
                    mine[0].active = True
                else:
                    self.env["o3p.mcp.action"].create(server_action._o3p_mcp_action_values())

    def _o3p_mcp_action_values(self):
        """Return the values of a new exposed action of this server action:
        named after it, for one record per call."""
        self.ensure_one()
        slug = re.sub(r"[^a-z0-9]+", "-", (self.name or "").lower()).strip("-")[:48].strip("-")
        if not slug or not slug[0].isalpha():
            slug = f"action-{slug}".strip("-")
        Action = self.env["o3p.mcp.action"].with_context(active_test=False)
        name = slug
        if Action.search_count([("name", "=", name)]):
            name = f"{slug}-{self.id}"
        return {
            "name": name,
            "title": self.name,
            "description": self.env._("Runs the server action %(name)s.", name=self.name),
            "kind": "server",
            "server_action_id": self.id,
        }
