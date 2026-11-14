import os

from odoo.tests import tagged

from ..models.oauth import set_config
from .sql_attacks import ATTACKS
from .sql_common import CONFIG_MARKER, FIELD_MARKER, HIDDEN_MARKER, PASSWORD_MARKER, O3pMcpSqlCase


STATUS = {"rows": 200, "error": 422, "refused": 422, "timeout": 504}


@tagged("post_install", "-at_install")
class TestSqlAttacks(O3pMcpSqlCase):
    """Every escape attempt fails, and no secret ever reaches the attacker."""

    def markers(self, sql, user):
        Partner = self.env["res.partner"].with_context(active_test=False)
        everything = Partner.search([("name", "=like", f"{HIDDEN_MARKER}%")])
        visible = Partner.with_user(self.users[user]).search([("id", "in", everything.ids)])
        values = [
            *(everything - visible).mapped("name"),
            CONFIG_MARKER,
            FIELD_MARKER,
            PASSWORD_MARKER,
            "root:x:0:",
            "uid=",
            "POSTGRES_PASSWORD",
            "sandbox-admin-test",
            os.environ["O3P_SQL_PASSWORD"],
            self.env.cr.dbname,
        ]
        # A marker the attacker typed is not a leak.
        return [value for value in values if value not in sql]

    def run_attacks(self, user):
        set_config(self.env, "o3p_mcp.api_timeout_seconds", 2)
        hidden = self.hidden2.id
        outcomes = {}
        with self.protected_field("res.partner", "comment"):
            for attack in ATTACKS:
                name, sql, expect = attack[:3]
                check = attack[3] if len(attack) > 3 else None
                sql = sql.replace("{db}", self.env.cr.dbname).replace("{hidden}", str(hidden))
                expected = {expect} if isinstance(expect, str) else expect
                with self.subTest(attack=name, user=user):
                    response = self.sql_call(sql, user=user)
                    text = response.text
                    for marker in self.markers(sql, user):
                        self.assertNotIn(marker, text, f"{name}: {text[:500]}")
                    self.assertIn(response.status_code, {STATUS[item] for item in expected}, f"{name}: {text[:500]}")
                    body = response.json()
                    if response.status_code == 422:
                        error = body["error"]
                        self.assertEqual(error["code"], "query_error")
                        if expected == {"refused"}:
                            self.assertIn(error["details"]["sqlstate"], ("0A000", "42601"), text)
                    if check and response.status_code == 200:
                        check(self, body)
                    outcomes[name] = response.status_code
        return outcomes

    def test_attacks_as_restricted_user(self):
        outcomes = self.run_attacks("restricted")
        self.assertEqual(len(outcomes), len(ATTACKS))
        # Odoo is unharmed: its own queries still work and nothing was written.
        self.assertEqual(self.sql_result("SELECT count(*) FROM res_partner WHERE id = 0")["rows"], [[0]])
        self.assertTrue(self.hidden2.exists())

    def test_attacks_as_second_restricted_user(self):
        self.run_attacks("restricted2")
