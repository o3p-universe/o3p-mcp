# Before the upgrade to 1.46 (scripts/check-upgrade.sh): a row in the call
# log and a list of writable models, which the upgrade must leave as they are.
env = env  # noqa: F821, PLW0127 - provided by odoo shell
env["ir.config_parameter"].sudo().set_str("o3p_mcp.api_writable_models", "res.partner\nres.partner.category")
env["o3p.mcp.api.log"].sudo().create(
    {"op": "update", "model": "res.partner", "status": 200, "row_count": 1, "record_ids": "7", "field_names": "ref"}
)
print("before", env["o3p.mcp.api.log"].sudo().search_count([]), "o3p.mcp.action" in env.registry)
env.cr.commit()
