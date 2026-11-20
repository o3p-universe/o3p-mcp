import importlib.util

from odoo import Command
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import file_path

from .common import O3pMcpCommon


@tagged("post_install", "-at_install")
class TestInstall(O3pMcpCommon, TransactionCase):
    def test_groups_and_implication(self):
        category = self.env.ref("o3p_mcp.module_category_mcp")
        self.assertEqual(self.user_group.category_id, category)
        self.assertEqual(self.editor_group.category_id, category)
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
        # Its own category, so the user form offers it next to User/Editor.
        category = self.env.ref("o3p_mcp.module_category_mcp_sql")
        self.assertEqual(self.sql_group.category_id, category)
        name = f"sel_groups_{self.sql_group.id}"
        description = self.env["res.users"].fields_get([name])[name]
        self.assertEqual(description["type"], "selection")
        self.assertEqual([label for _value, label in description["selection"]][1:], ["Allowed"])
        oauth = self.env["o3p.mcp.oauth"]
        self.assertNotIn("odoo.sql", oauth._scopes_for_user(self.reader))
        # It adds odoo.sql to MCP / User and grants nothing without it, so
        # removing MCP / User ends every MCP right in one step.
        self.assertFalse(self.sql_group.implied_ids)
        self.assertFalse(self.sql_group.model_access)
        sql_only = new_test_user(
            self.env, "o3p_mcp_sql_only", groups="base.group_user,o3p_mcp.group_mcp_sql"
        )
        self.assertEqual(oauth._scopes_for_user(sql_only), set())
        sql_only.write({"groups_id": [Command.link(self.user_group.id)]})
        self.assertEqual(
            oauth._scopes_for_user(sql_only), {"okf.read", "odoo.read", "odoo.sql"}
        )
        sql_only.write({"groups_id": [Command.unlink(self.user_group.id)]})
        self.assertEqual(oauth._scopes_for_user(sql_only), set())

    def test_upgrade_keeps_sql_for_mcp_users(self):
        spec = importlib.util.spec_from_file_location(
            "o3p_mcp_post_migrate_sql_group",
            file_path("o3p_mcp/migrations/18.0.1.41.0/post-migrate.py"),
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
            script.migrate(self.env.cr, "18.0.1.40.1")
            self.env.invalidate_all()
            for user in (self.reader, editor_only, archived, self.editor):
                self.assertIn(self.sql_group, user.groups_id, user.login)
            self.assertNotIn(self.sql_group, self.plain.groups_id)
            self.assertNotIn(self.sql_group, portal.groups_id)
        self.assertIn("odoo.sql", self.env["o3p.mcp.oauth"]._scopes_for_user(self.reader))

    def test_mcp_groups_grant_nothing_else(self):
        # Odoo 18 has no light or regular users: the user type is internal or
        # portal (share). The MCP groups imply no other group, so holding them
        # changes neither the type nor any right on Odoo records.
        self.assertFalse(self.user_group.implied_ids)
        self.assertEqual(self.editor_group.implied_ids, self.user_group)
        self.assertFalse(self.editor.share)
        self.assertFalse(self.user_group.model_access)
        self.assertFalse(self.editor_group.model_access)

    def test_user_form_offers_one_mcp_selection(self):
        # Odoo 18 turns a category whose groups imply each other into one
        # selection field on the user form.
        name = "sel_groups_" + "_".join(
            str(group_id) for group_id in sorted((self.user_group | self.editor_group).ids)
        )
        description = self.env["res.users"].fields_get([name])[name]
        self.assertEqual(description["type"], "selection")
        self.assertEqual(
            [label for _value, label in description["selection"]][1:], ["User", "Editor"]
        )
        self.assertEqual(self.editor.read([name])[0][name], self.editor_group.id)

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
