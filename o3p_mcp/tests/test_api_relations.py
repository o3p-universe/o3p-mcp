"""Text conditions on many2one fields apply the related model's rules.

Odoo matches ``partner_id ilike "x"`` (or ``in ["x"]``) against the related
records' names, emails, VAT numbers…; on a relation that bypasses access it
does so without the related model's rules. The API rewrites text operands to
``any`` on ``display_name`` and refuses them on relations that bypass access.
"""

import unittest

from odoo import Command
from odoo.tests import new_test_user, tagged

from .api_common import O3pMcpApiCase


@tagged("post_install", "-at_install")
class TestManyToOneNames(O3pMcpApiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        visible = [Command.link(cls.visible.id)]
        cls.visible_child = Partner.create(
            {"name": "MCPT Visible Child", "parent_id": cls.alpha.id, "category_id": visible}
        )
        cls.hidden_parent = Partner.create(
            {"name": "MCPT Hidden Parent", "email": "hidden-parent@secret.example"}
        )
        cls.orphan = Partner.create(
            {"name": "MCPT Orphan", "parent_id": cls.hidden_parent.id, "category_id": visible, "type": "other"}
        )
        cls.leaky = new_test_user(
            cls.env, "o3p_mcp_leaky", groups="base.group_user", name="MCPT Leaky",
            email="leakprobe@secret.example",
        )

    def names(self, domain, user="restricted"):
        result = self.result(
            "search",
            {"model": "res.partner", "domain": [["name", "=like", "MCPT %"], *domain], "fields": ["name"]},
            user=user,
        )
        return sorted(row["name"] for row in result["records"])

    def test_readable_names_still_match(self):
        for domain in (
            [["parent_id", "ilike", "MCPT Alpha"]],
            [["parent_id", "=", "MCPT Alpha"]],
            [["parent_id", "in", ["MCPT Alpha"]]],
            [["parent_id", "=", self.alpha.id]],
        ):
            with self.subTest(domain=domain):
                self.assertEqual(self.names(domain), ["MCPT Visible Child"])
        self.assertIn("MCPT Alpha", self.names([["parent_id", "in", [self.alpha.id, False]]]))
        self.assertNotIn("MCPT Visible Child", self.names([["parent_id", "not ilike", "MCPT Alpha"]]))

    def test_hidden_names_find_nothing(self):
        for domain in (
            [["parent_id", "ilike", "MCPT Hidden Parent"]],
            [["parent_id", "ilike", "hidden-parent@secret"]],
            [["parent_id", "in", ["MCPT Hidden Parent"]]],
        ):
            with self.subTest(domain=domain):
                self.assertEqual(self.names(domain), [])
                self.assertEqual(self.names(domain, "admin"), ["MCPT Orphan"])

    def test_text_on_relations_bypassing_access_is_refused(self):
        # res.users.partner_id bypasses access to res.partner.
        for domain in (
            [["partner_id", "ilike", "leakprobe@secret"]],
            [["partner_id", "in", ["MCPT Leaky"]]],
            [["partner_id", "=", "MCPT Leaky"]],
        ):
            with self.subTest(domain=domain):
                self.assertApiError(
                    self.call("search", {"model": "res.users", "domain": domain, "fields": ["login"]}),
                    "invalid_params",
                )
        # Ids compare the column and remain accepted.
        result = self.result(
            "search",
            {"model": "res.users", "domain": [["partner_id", "=", self.leaky.partner_id.id]], "fields": ["login"]},
            user="admin",
        )
        self.assertEqual([row["login"] for row in result["records"]], ["o3p_mcp_leaky"])

    def test_mixed_or_unsupported_operands_are_refused(self):
        for condition in (
            ["parent_id", "in", [self.alpha.id, "MCPT Alpha"]],
            ["parent_id", "ilike", 3],
            ["parent_id", ">", "MCPT"],
            ["parent_id", "=?", "MCPT Alpha"],
        ):
            with self.subTest(condition=condition):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "domain": [condition], "fields": ["name"]}),
                    "invalid_params",
                )


@tagged("post_install", "-at_install")
class TestProjectCustomerProbe(O3pMcpApiCase):
    """The review probe: project.project.partner_id bypasses access."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "project.project" not in cls.env:
            raise unittest.SkipTest("project is not installed")
        env = cls.env
        cls.restricted.write({"groups_id": [Command.link(env.ref("project.group_project_user").id)]})
        hidden = env["res.partner"].create(
            {"name": "MCPT Hidden Customer", "email": "leakprobe2@secret.example", "vat": "IT12345678901"}
        )
        cls.project = env["project.project"].create(
            {"name": "MCPT Project", "partner_id": hidden.id, "privacy_visibility": "employees"}
        )
        cls.hidden_customer = hidden

    def found(self, domain):
        response = self.call("search", {"model": "project.project", "domain": domain, "fields": ["name"]})
        if response.status_code == 400:
            return []
        self.assertEqual(response.status_code, 200, response.text)
        return [row["name"] for row in response.json()["result"]["records"]]

    def test_email_and_vat_probes_find_nothing(self):
        self.assertEqual(self.found([["partner_id", "ilike", "leakprobe2@secret"]]), [])
        self.assertEqual(self.found([["partner_id", "in", ["IT12345678901"]]]), [])
        self.assertEqual(self.found([["partner_id", "=", self.hidden_customer.id]]), ["MCPT Project"])
