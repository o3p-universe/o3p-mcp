# Run in `odoo shell` on the lab database (lab.sh knowledge): sets up company
# knowledge for several audiences. Two audiences follow the sales groups:
# `sales` (the salespeople and, through her group, their manager) and
# `management` (the sales manager and the administrator who holds that
# group). One salesperson and the manager become knowledge editors. Prints
# one line "LAB_KNOWLEDGE <json>".
import json

from odoo import Command

env = env  # noqa: F821, PLW0127 - provided by odoo shell
Audience = env["o3p.mcp.audience"]
wanted = {
    "sales": ("Sales", "sales_team.group_sale_salesman"),
    "management": ("Management", "sales_team.group_sale_manager"),
}
for code, (name, group) in wanted.items():
    audience = Audience.search([("code", "=", code)]) or Audience.create({"name": name, "code": code})
    audience.group_ids = [Command.set([env.ref(group).id])]
editor = env.ref("o3p_mcp.group_mcp_editor")
writers = env["res.users"].search(
    [("login", "in", ["sara.vendite@example.test", "marta.capo@example.test"])]
)
writers.write({"group_ids": [Command.link(editor.id)]})
oauth = env["o3p.mcp.oauth"]
users = env["res.users"].search([("login", "like", "%@example.test")])
print(
    "LAB_KNOWLEDGE "
    + json.dumps(
        {
            "audiences": {user.login: oauth._audiences_for_user(user) for user in users},
            "editors": sorted(writers.mapped("login")),
        }
    )
)
env.cr.commit()
