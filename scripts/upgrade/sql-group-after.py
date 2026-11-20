# After the upgrade to 1.41 (scripts/check-upgrade.sh): every internal user
# who held MCP / User, directly or through MCP / Editor, holds MCP SQL /
# Allowed and so keeps odoo.sql; nobody else received it.
env = env  # noqa: F821, PLW0127 - provided by odoo shell
sql = env.ref("o3p_mcp.group_mcp_sql")
oauth = env["o3p.mcp.oauth"]
Users = env["res.users"].with_context(active_test=False)
expected = {
    # login: (holds the SQL group, scopes)
    "up_user": (True, {"okf.read", "odoo.read", "odoo.sql"}),
    "up_editor": (True, {"okf.read", "okf.write", "odoo.read", "odoo.sql"}),
    "up_plain": (False, set()),
    "up_archived": (True, set()),
}
problems = []
for login, (has_sql, scopes) in expected.items():
    user = Users.search([("login", "=", login)])
    got = (sql in user.groups_id, oauth._scopes_for_user(user))
    print("after", login, got[0], sorted(got[1]))
    if got != (has_sql, scopes):
        problems.append(login)
others = Users.search([("login", "not like", "up_%"), ("groups_id", "in", sql.ids)])
if others:
    problems.append(f"SQL group also given to {others.mapped('login')}")
print("UPGRADE OK" if not problems else f"UPGRADE FAILED: {problems}")
