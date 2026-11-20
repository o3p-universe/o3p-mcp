# Up to 1.40 the MCP / User group granted the odoo.sql scope. SQL is a group of
# its own now (MCP SQL / Allowed), which adds the scope for users who hold
# MCP / User: give it to the internal users who hold MCP / User, directly or
# through MCP / Editor, so that nobody loses SQL by upgrading. Running it again
# changes nothing.
from odoo import SUPERUSER_ID, Command, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    user_group = env.ref("o3p_mcp.group_mcp_user", raise_if_not_found=False)
    sql_group = env.ref("o3p_mcp.group_mcp_sql", raise_if_not_found=False)
    if not user_group or not sql_group:
        return
    users = (
        env["res.users"]
        .with_context(active_test=False)
        .search(
            [
                ("share", "=", False),
                ("all_group_ids", "in", user_group.ids),
                ("all_group_ids", "not in", sql_group.ids),
            ]
        )
    )
    users.write({"group_ids": [Command.link(sql_group.id)]})
