# Run in `odoo shell` on the lab database (lab.sh writes): opens record
# writing for the lab. The two salespeople and their manager get MCP records /
# Editor and Odoo's own right to create contacts; contacts, sales orders and
# their lines become writable. The other users keep reading only. Prints one
# line "LAB_WRITES <json>".
import json

from odoo import Command

env = env  # noqa: F821, PLW0127 - provided by odoo shell
editor = env.ref("o3p_mcp.group_mcp_record_editor")
logins = ["sara.vendite@example.test", "bruno.vendite@example.test", "marta.capo@example.test"]
users = env["res.users"].search([("login", "in", logins)])
contact_creation = env.ref("base.group_partner_manager")
users.write({"group_ids": [Command.link(editor.id), Command.link(contact_creation.id)]})
models = ["res.partner", "sale.order", "sale.order.line"]
env["ir.config_parameter"].sudo().set_str("o3p_mcp.api_writable_models", "\n".join(models))
print("LAB_WRITES " + json.dumps({"editors": sorted(users.mapped("login")), "models": models}))
env.cr.commit()
