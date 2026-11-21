# After the upgrade to 1.46 or later (scripts/check-upgrade.sh): no action is
# exposed, so nothing can be run; the call log kept its rows and the writable
# models are the same.
env = env  # noqa: F821, PLW0127 - provided by odoo shell
problems = []
exposed = env["o3p.mcp.action"].with_context(active_test=False).search_count([])
if exposed:
    problems.append(f"{exposed} actions are exposed")
admin = env.ref("base.user_admin")
if env["o3p.mcp.api"].with_user(admin)._runnable_actions():
    problems.append("the administrator is listed actions")
row = env["o3p.mcp.api.log"].sudo().search([("op", "=", "update"), ("record_ids", "=", "7")])
if len(row) != 1 or row.field_names != "ref" or row.action:
    problems.append(f"the call-log row changed: {row.read(['field_names', 'action'])}")
writable = env["ir.config_parameter"].sudo().get_param("o3p_mcp.api_writable_models", "")
if writable != "res.partner\nres.partner.category":
    problems.append(f"the writable models changed: {writable!r}")
if not env.ref("o3p_mcp.menu_o3p_mcp_action", raise_if_not_found=False):
    problems.append("the Exposed actions menu is missing")
print("after", exposed, len(row), repr(writable))
print("UPGRADE OK" if not problems else f"UPGRADE FAILED: {problems}")
