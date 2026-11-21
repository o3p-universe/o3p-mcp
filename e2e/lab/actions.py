# Run in `odoo shell` on the lab database (lab.sh actions): exposes three
# actions on sales orders and opens record writing as lab.sh writes does, so
# that a salesperson can create the quotation the actions then run on. The
# two salespeople and their manager get MCP records / Editor. Prints one line
# "LAB_ACTIONS <json>".
import json

from odoo import Command

env = env  # noqa: F821, PLW0127 - provided by odoo shell
editor = env.ref("o3p_mcp.group_mcp_record_editor")
logins = ["sara.vendite@example.test", "bruno.vendite@example.test", "marta.capo@example.test"]
users = env["res.users"].search([("login", "in", logins)])
users.write(
    {"group_ids": [Command.link(editor.id), Command.link(env.ref("base.group_partner_manager").id)]}
)
models = ["res.partner", "sale.order", "sale.order.line"]
env["ir.config_parameter"].sudo().set_str("o3p_mcp.api_writable_models", "\n".join(models))

order = env["ir.model"]._get("sale.order")
Action = env["o3p.mcp.action"]
Action.with_context(active_test=False).search([]).unlink()
checked = env["ir.actions.server"].create(
    {
        "name": "Lab: mark as checked by the manager",
        "model_id": order.id,
        "state": "code",
        "code": "records.write({'client_order_ref': 'CHECKED'})",
    }
)
actions = Action.create(
    [
        {
            "name": "confirm-quotation",
            "title": "Confirm a quotation",
            "description": "Confirms a quotation the customer has accepted: it becomes a sales "
            "order. Only a quotation that is still a draft or was sent can be confirmed.",
            "kind": "method",
            "model_id": order.id,
            "method": "action_confirm",
            "max_records": 5,
        },
        {
            "name": "discount-quotation",
            "title": "Discount a quotation",
            "description": "Gives a discount on a quotation. For the same percentage on every "
            "line use discount_type sol_discount and discount_percentage as a fraction "
            "(0.1 for 10%).",
            "kind": "wizard",
            "model_id": order.id,
            "wizard_model_id": env["ir.model"]._get("sale.order.discount").id,
            "record_field": "sale_order_id",
            "steps": "action_apply_discount",
            "inputs": "discount_type, discount_percentage, discount_amount",
        },
        {
            "name": "mark-checked",
            "title": "Mark orders as checked",
            "description": "Writes CHECKED as the customer reference of the orders. For sales "
            "managers only.",
            "kind": "server",
            "server_action_id": checked.id,
            "max_records": 10,
            "group_ids": [Command.link(env.ref("sales_team.group_sale_manager").id)],
        },
    ]
)
problems = {action.name: action.problem for action in actions if action.problem}
print(
    "LAB_ACTIONS "
    + json.dumps(
        {
            "editors": sorted(users.mapped("login")),
            "models": models,
            "actions": {action.name: action.summary for action in actions},
            "problems": problems,
        }
    )
)
env.cr.commit()
