from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestInstall(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oauth = cls.env["o3p.mcp.oauth"]
        cls.plain = new_test_user(cls.env, "o3p_mcp_plain", groups="base.group_user")
        cls.reader = new_test_user(
            cls.env,
            "o3p_mcp_reader",
            groups="base.group_user,o3p_mcp.group_mcp_user",
        )
        cls.editor = new_test_user(
            cls.env,
            "o3p_mcp_editor",
            groups="base.group_user,o3p_mcp.group_mcp_editor",
        )

    def test_groups_and_implication(self):
        privilege = self.env.ref("o3p_mcp.res_groups_privilege_mcp")
        user_group = self.env.ref("o3p_mcp.group_mcp_user")
        editor_group = self.env.ref("o3p_mcp.group_mcp_editor")
        self.assertEqual(user_group.privilege_id, privilege)
        self.assertEqual(editor_group.privilege_id, privilege)
        self.assertIn(user_group, editor_group.implied_ids)
        self.assertTrue(self.editor.has_group("o3p_mcp.group_mcp_user"))
        self.assertFalse(self.reader.has_group("o3p_mcp.group_mcp_editor"))

    def test_mcp_groups_keep_light_users_light(self):
        self.assertFalse(self.editor.has_group("base.group_user_regular"))
        self.assertEqual(self.editor.role, "light_user")

    def test_scopes_for_user(self):
        self.assertEqual(self.oauth._scopes_for_user(self.plain), set())
        self.assertEqual(self.oauth._scopes_for_user(self.reader), {"okf.read"})
        self.assertEqual(
            self.oauth._scopes_for_user(self.editor), {"okf.read", "okf.write"}
        )

    def test_scopes_need_an_active_internal_user(self):
        self.assertEqual(
            self.oauth._scopes_for_user(self.env.ref("base.public_user")), set()
        )
        self.editor.active = False
        self.assertEqual(self.oauth._scopes_for_user(self.editor), set())
