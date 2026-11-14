from odoo.tests import TransactionCase, new_test_user, tagged

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
            {"okf.read", "okf.write", "odoo.read", "odoo.sql"},
        )
        self.assertFalse(self.reader.has_group("o3p_mcp.group_mcp_editor"))

    def test_mcp_groups_grant_nothing_else(self):
        # Odoo 18 has no light or regular users: the user type is internal or
        # portal (share). The MCP groups imply no other group, so holding them
        # changes neither the type nor any right on Odoo records.
        self.assertFalse(self.user_group.implied_ids)
        self.assertEqual(self.editor_group.implied_ids, self.user_group)
        self.assertFalse(self.editor.share)
        self.assertFalse(self.user_group.model_access)
        self.assertFalse(self.editor_group.model_access)

    def test_scopes_for_user(self):
        oauth = self.env["o3p.mcp.oauth"]
        self.assertEqual(oauth._scopes_for_user(self.plain), set())
        self.assertEqual(
            oauth._scopes_for_user(self.reader), {"okf.read", "odoo.read", "odoo.sql"}
        )
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
