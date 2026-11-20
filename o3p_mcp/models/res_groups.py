from odoo import models


class ResGroups(models.Model):
    _inherit = "res.groups"

    def _get_light_group_xmlids(self):
        # The MCP groups grant no rights on Odoo records, so holding one must
        # not turn a light user into a regular user.
        return (
            *super()._get_light_group_xmlids(),
            "o3p_mcp.group_mcp_user",
            "o3p_mcp.group_mcp_editor",
            "o3p_mcp.group_mcp_sql",
            "o3p_mcp.group_mcp_record_editor",
        )
