from odoo.tests import tagged

from ..models.api import ApiError
from .sql_common import O3pMcpSqlCase


@tagged("post_install", "-at_install")
class TestSqlMatchesOrm(O3pMcpSqlCase):
    """For every table and every test user, SQL sees what the ORM shows.

    A model table answers ``count(*)`` equal to ``search_count`` with
    ``active_test=False`` as the user, or does not exist for that user when
    the model is hidden or unreadable. A many2many table holds exactly the
    pairs the ORM reads through the field, from the records the user may
    read to the records the user may read.
    """

    def expect_missing(self, result, table, user):
        self.assertIsInstance(result, ApiError, f"{table} should not exist for {user}")
        self.assertEqual(
            (result.code, result.details and result.details["sqlstate"]),
            ("query_error", "42P01"),
            f"{table} for {user}: {result.details}",
        )

    def test_every_model_table(self):
        index = self.env["o3p.mcp.api"]._sql_table_index()
        exposed = set()
        for table, (kind, items) in sorted(index.items()):
            if kind != "model":
                continue
            for name, user in self.users.items():
                with self.subTest(table=table, user=name):
                    result = self.direct(user, f'SELECT count(*) FROM "{table}"')
                    api = self.env["o3p.mcp.api"].with_user(user)
                    model = api._sql_model(items[0]) if len(items) == 1 else None
                    if model is None:
                        self.expect_missing(result, table, name)
                        continue
                    exposed.add(items[0])
                    self.assertNotIsInstance(result, ApiError, f"{table} for {name}: {getattr(result, 'details', None)}")
                    self.assertEqual(result["rows"], [[model.search_count([])]], f"{table} for {name}")
        # The comparison covers the models users work with.
        self.assertLessEqual({"res.partner", "res.users", "res.company", "res.partner.category"}, exposed)
        for hidden in ("ir.config_parameter", "ir.attachment", "o3p.mcp.token", "res.users.apikeys"):
            self.assertNotIn(hidden, exposed)

    def test_every_many2many_table(self):
        index = self.env["o3p.mcp.api"]._sql_table_index()
        checked = set()
        for table, (kind, entries) in sorted(index.items()):
            if kind != "m2m":
                continue
            for name, user in self.users.items():
                with self.subTest(table=table, user=name):
                    api = self.env["o3p.mcp.api"].with_user(user)
                    exposed = api._sql_relation_select(table, entries) is not None
                    result = self.direct(user, f'SELECT count(*) FROM "{table}"')
                    if not exposed:
                        self.expect_missing(result, table, name)
                        continue
                    model_name, field_name = entries[0]
                    # Odoo 18 caches x2many values per record for every user.
                    self.env.invalidate_all()
                    records = self.env[model_name].with_user(user).with_context(active_test=False).search([])
                    pairs = sum(len(record[field_name]) for record in records)
                    self.assertEqual(result["rows"], [[pairs]], f"{table} for {name}")
                    checked.add(table)
        self.assertIn("res_partner_res_partner_category_rel", checked)

    def test_inherits_parent_rights_apply(self):
        # res.users is stored partly in res.partner: a user whose partner is
        # hidden is hidden too, as in the ORM.
        for name, user in self.users.items():
            with self.subTest(user=name):
                users = self.env["res.users"].with_user(user).with_context(active_test=False)
                result = self.direct(user, "SELECT id FROM res_users ORDER BY id")
                self.assertEqual([row[0] for row in result["rows"]], users.search([], order="id").ids)
        restricted = self.direct(self.restricted, "SELECT count(*) FROM res_users")["rows"][0][0]
        admin = self.direct(self.admin, "SELECT count(*) FROM res_users")["rows"][0][0]
        self.assertLess(restricted, admin)
