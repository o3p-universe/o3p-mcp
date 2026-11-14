# Configure the end-to-end database. Run inside `odoo shell -d o3p_e2e`
# (stdin), with E2E_ISSUER and E2E_MCP_URL in the environment. Prints one
# line "E2E_SETUP <json>" with the introspection credentials and the users.
#
# The settings and the introspection credentials go through the same
# methods as the Settings › MCP form and its "Create or rotate credentials"
# button; the secret is read from the notification an administrator sees.
import json
import os
import re

from odoo import Command

issuer = os.environ["E2E_ISSUER"]
mcp_url = os.environ["E2E_MCP_URL"]
password = os.environ.get("E2E_PASSWORD", "e2e-Passw0rd!")

params = env["ir.config_parameter"].sudo()  # noqa: F821 - provided by odoo shell
params.set_str("web.base.url", issuer)
params.set_bool("web.base.url.freeze", True)

settings = env["res.config.settings"].create(  # noqa: F821
    {"o3p_mcp_issuer": issuer, "o3p_mcp_resource_url": mcp_url}
)
settings.execute()
notification = settings.action_o3p_mcp_rotate_credentials()
message = notification["params"]["message"]
match = re.search(r"Client ID: (\S+) .* Client secret: (\S+) ", message)
if not match:
    raise SystemExit("could not read the introspection credentials")
client_id, client_secret = match.groups()

ref = env.ref  # noqa: F821
internal = ref("base.group_user")
portal = ref("base.group_portal")
system = ref("base.group_system")
mcp_user = ref("o3p_mcp.group_mcp_user")
mcp_editor = ref("o3p_mcp.group_mcp_editor")

# key: (login, name, email, groups)
people = {
    "editor": ("eva.editor@example.test", "Niccolò D'Amico", "eva.editor@example.test",
               [internal, mcp_user, mcp_editor]),
    "editor2": ("ezio.editor@example.test", "Ezio Revisore", "ezio@example.test",
                [internal, mcp_user, mcp_editor]),
    "user": ("ugo.user@example.test", "Ugo Utente", "ugo.user@example.test",
             [internal, mcp_user]),
    "internal": ("ivo.internal@example.test", "Ivo Interno", "ivo@example.test",
                 [internal]),
    "portal": ("pia.portal@example.test", "Pia Portale", "pia@example.test",
               [portal]),
    "admin": ("ada.admin@example.test", "Ada Admin", "ada@example.test",
              [internal, system]),
}
Users = env["res.users"].with_context(no_reset_password=True)  # noqa: F821
users = {}
for key, (login, name, email, groups) in people.items():
    user = Users.create(
        {
            "login": login,
            "name": name,
            "email": email,
            "password": password,
            "group_ids": [Command.set([group.id for group in groups])],
        }
    )
    users[key] = {"id": user.id, "login": login, "name": name, "email": email}

# README: portal users hold no scope "even when placed in an MCP group".
# Odoo may refuse to mix a portal user with an internal-only group; record
# what happened instead of failing the setup.
portal_user = env["res.users"].browse(users["portal"]["id"])  # noqa: F821
try:
    with env.cr.savepoint():  # noqa: F821
        portal_user.write({"group_ids": [Command.link(mcp_user.id)]})
        portal_user.flush_recordset()
    users["portal"]["mcp_group"] = mcp_user in portal_user.all_group_ids
except Exception as error:  # noqa: BLE001
    users["portal"]["mcp_group"] = False
    users["portal"]["mcp_group_error"] = type(error).__name__

env.cr.commit()  # noqa: F821
print(
    "E2E_SETUP "
    + json.dumps(
        {
            "issuer": env["o3p.mcp.oauth"]._issuer(),  # noqa: F821
            "resource_url": env["o3p.mcp.oauth"]._resource_url(),  # noqa: F821
            "introspection": {"client_id": client_id, "client_secret": client_secret},
            "password": password,
            "groups": {"mcp_user": mcp_user.id, "mcp_editor": mcp_editor.id},
            "users": users,
        }
    )
)
