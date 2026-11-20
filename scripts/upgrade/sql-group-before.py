# Before the upgrade to 1.41 (scripts/check-upgrade.sh): users as an
# installation of 1.40 has them, when MCP / User still granted odoo.sql.
from odoo import Command

env = env  # noqa: F821, PLW0127 - provided by odoo shell
internal = env.ref("base.group_user")
mcp_user = env.ref("o3p_mcp.group_mcp_user")
mcp_editor = env.ref("o3p_mcp.group_mcp_editor")
Users = env["res.users"].with_context(no_reset_password=True)
for login, groups in (
    ("up_user", internal + mcp_user),
    ("up_editor", internal + mcp_editor),
    ("up_plain", internal),
    ("up_archived", internal + mcp_user),
):
    Users.create({"name": login, "login": login, "group_ids": [Command.set(groups.ids)]})
Users.search([("login", "=", "up_archived")]).active = False
oauth = env["o3p.mcp.oauth"]
for user in Users.with_context(active_test=False).search([("login", "like", "up_%")]):
    print("before", user.login, sorted(oauth._scopes_for_user(user)))
env.cr.commit()
