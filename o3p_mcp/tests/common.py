from odoo import Command
from odoo.tests import new_test_user


RESOURCE = "https://mcp.example.com/mcp"
REDIRECT_URI = "https://client.example.com/callback"


class O3pMcpCommon:
    """Users, groups and a public client shared by the o3p_mcp tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_group = cls.env.ref("o3p_mcp.group_mcp_user")
        cls.editor_group = cls.env.ref("o3p_mcp.group_mcp_editor")
        cls.plain = new_test_user(
            cls.env, "o3p_mcp_plain", groups="base.group_user", name="Plain User"
        )
        cls.reader = new_test_user(
            cls.env,
            "o3p_mcp_reader",
            groups="base.group_user,o3p_mcp.group_mcp_user",
            name="MCP Reader",
        )
        cls.editor = new_test_user(
            cls.env,
            "o3p_mcp_editor",
            groups="base.group_user,o3p_mcp.group_mcp_user,o3p_mcp.group_mcp_editor",
            name="MCP Editor",
        )
        cls.client = cls.env["o3p.mcp.client"].create(
            {
                "name": "Test MCP client",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
            }
        )
        cls.Token = cls.env["o3p.mcp.token"]
        cls.Code = cls.env["o3p.mcp.code"]

    @classmethod
    def demote_to_reader(cls, user):
        user.write(
            {
                "group_ids": [
                    Command.unlink(cls.editor_group.id),
                    Command.link(cls.user_group.id),
                ]
            }
        )

    @classmethod
    def remove_mcp_access(cls, user):
        user.write(
            {
                "group_ids": [
                    Command.unlink(cls.editor_group.id),
                    Command.unlink(cls.user_group.id),
                ]
            }
        )
