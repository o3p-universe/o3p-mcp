from psycopg2 import errors as pg_errors

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from .common import O3pMcpCommon


@tagged("post_install", "-at_install")
class TestAudience(O3pMcpCommon, TransactionCase):
    def audience(self, code, *groups, **values):
        return self.env["o3p.mcp.audience"].create(
            {
                "name": code.title(),
                "code": code,
                "group_ids": [Command.set([group.id for group in groups])],
                **values,
            }
        )

    def test_codes_are_names_the_mcp_server_can_use(self):
        for code in ("hr", "sales-team", "a", "level_2", "x" * 32):
            with self.subTest(code=code):
                self.audience(code)
        for code in ("HR", "1hr", "h r", "hr.", "-hr", "x" * 33, "hr/..", "ünì"):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                self.audience(code)
        with self.assertRaises(pg_errors.UniqueViolation), mute_logger("odoo.sql_db"):
            self.audience("hr")

    def test_a_user_belongs_through_any_group(self):
        oauth = self.env["o3p.mcp.oauth"]
        internal = self.env.ref("base.group_user")
        self.audience("staff", internal)
        self.audience("editors", self.editor_group, self.env.ref("base.group_system"))
        self.audience("archived", internal, active=False)
        self.assertEqual(oauth._audiences_for_user(self.reader), ["staff"])
        self.assertEqual(oauth._audiences_for_user(self.editor), ["editors", "staff"])
        # Only users who may sign in belong to an audience.
        self.assertEqual(oauth._audiences_for_user(self.plain), [])
        portal = new_test_user(
            self.env, "o3p_mcp_audience_portal", groups="base.group_portal,o3p_mcp.group_mcp_user"
        )
        self.audience("portal", self.env.ref("base.group_portal"))
        self.assertEqual(oauth._audiences_for_user(portal), [])
        self.editor.active = False
        self.assertEqual(oauth._audiences_for_user(self.editor), [])

    def test_at_most_64_audiences_are_named(self):
        internal = self.env.ref("base.group_user")
        for number in range(70):
            self.audience(f"a{number:02d}", internal)
        names = self.env["o3p.mcp.oauth"]._audiences_for_user(self.reader)
        self.assertEqual(len(names), 64)
        self.assertEqual(names, sorted(names))
        self.assertEqual(names[0], "a00")

    def test_user_count(self):
        staff = self.audience("staff", self.editor_group)
        self.assertEqual(staff.user_count, 1)
        self.assertEqual(self.audience("nobody").user_count, 0)

    def test_audiences_are_hidden_from_the_data_api(self):
        self.assertTrue(self.env["o3p.mcp.api"]._model_denied("o3p.mcp.audience"))
