import importlib.util

from odoo import Command
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import file_path

from .common import O3pMcpCommon


@tagged("post_install", "-at_install")
class TestInstall(O3pMcpCommon, TransactionCase):
    def test_groups_and_implication(self):
        privilege = self.env.ref("o3p_mcp.res_groups_privilege_mcp")
        self.assertEqual(self.user_group.privilege_id, privilege)
        self.assertEqual(self.editor_group.privilege_id, privilege)
        self.assertIn(self.user_group, self.editor_group.implied_ids)
        editor_only = new_test_user(
            self.env,
            "o3p_mcp_editor_only",
            groups="base.group_user,o3p_mcp.group_mcp_editor",
        )
        self.assertTrue(editor_only.has_group("o3p_mcp.group_mcp_user"))
        self.assertEqual(
            self.env["o3p.mcp.oauth"]._scopes_for_user(editor_only),
            {"okf.read", "okf.write", "odoo.read"},
        )
        self.assertFalse(self.reader.has_group("o3p_mcp.group_mcp_editor"))

    def test_sql_is_a_group_of_its_own(self):
        # Its own privilege, so the user form offers it next to User/Editor.
        privilege = self.env.ref("o3p_mcp.res_groups_privilege_mcp_sql")
        self.assertEqual(self.sql_group.privilege_id, privilege)
        self.assertEqual(privilege.group_ids, self.sql_group)
        oauth = self.env["o3p.mcp.oauth"]
        self.assertNotIn("odoo.sql", oauth._scopes_for_user(self.reader))
        # It adds odoo.sql to MCP / User and grants nothing without it, so
        # removing MCP / User ends every MCP right in one step.
        self.assertFalse(self.sql_group.implied_ids)
        sql_only = new_test_user(
            self.env, "o3p_mcp_sql_only", groups="base.group_user,o3p_mcp.group_mcp_sql"
        )
        self.assertEqual(oauth._scopes_for_user(sql_only), set())
        self.assertEqual(sql_only.role, "light_user")
        sql_only.write({"group_ids": [Command.link(self.user_group.id)]})
        self.assertEqual(
            oauth._scopes_for_user(sql_only), {"okf.read", "odoo.read", "odoo.sql"}
        )
        sql_only.write({"group_ids": [Command.unlink(self.user_group.id)]})
        self.assertEqual(oauth._scopes_for_user(sql_only), set())

    def test_upgrade_keeps_sql_for_mcp_users(self):
        spec = importlib.util.spec_from_file_location(
            "o3p_mcp_post_migrate_sql_group",
            file_path("o3p_mcp/migrations/20.0.1.41.0/post-migrate.py"),
        )
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        editor_only = new_test_user(
            self.env,
            "o3p_mcp_editor_before_upgrade",
            groups="base.group_user,o3p_mcp.group_mcp_editor",
        )
        archived = new_test_user(
            self.env,
            "o3p_mcp_archived_before_upgrade",
            groups="base.group_user,o3p_mcp.group_mcp_user",
        )
        archived.active = False
        portal = new_test_user(
            self.env,
            "o3p_mcp_portal_before_upgrade",
            groups="base.group_portal,o3p_mcp.group_mcp_user",
        )
        for _run in range(2):
            script.migrate(self.env.cr, "20.0.1.40.1")
            self.env.invalidate_all()
            for user in (self.reader, editor_only, archived, self.editor):
                self.assertIn(self.sql_group, user.group_ids, user.login)
                self.assertEqual(len(user.group_ids & self.sql_group), 1)
            self.assertNotIn(self.sql_group, self.plain.all_group_ids)
            self.assertNotIn(self.sql_group, portal.all_group_ids)
        self.assertIn("odoo.sql", self.env["o3p.mcp.oauth"]._scopes_for_user(self.reader))

    def test_mcp_groups_keep_light_users_light(self):
        self.assertFalse(self.editor.has_group("base.group_user_regular"))
        self.assertEqual(self.editor.role, "light_user")

    def test_scopes_for_user(self):
        oauth = self.env["o3p.mcp.oauth"]
        self.assertEqual(oauth._scopes_for_user(self.plain), set())
        self.assertEqual(oauth._scopes_for_user(self.reader), {"okf.read", "odoo.read"})
        self.assertEqual(
            oauth._scopes_for_user(self.editor),
            {"okf.read", "okf.write", "odoo.read", "odoo.sql"},
        )

    def test_scopes_need_an_active_internal_user(self):
        oauth = self.env["o3p.mcp.oauth"]
        self.assertEqual(oauth._scopes_for_user(self.env.ref("base.public_user")), set())
        # Review probe: a portal user placed in the MCP groups.
        portal = new_test_user(
            self.env,
            "o3p_mcp_portal",
            groups="base.group_portal,o3p_mcp.group_mcp_editor",
            name="Portal User",
        )
        self.assertTrue(portal.share)
        self.assertTrue(portal.has_group("o3p_mcp.group_mcp_user"))
        self.assertEqual(oauth._scopes_for_user(portal), set())
        self.editor.active = False
        self.assertEqual(oauth._scopes_for_user(self.editor), set())
