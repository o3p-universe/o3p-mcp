"""Validation errors say why and where, with fixed texts only."""

from odoo.tests import tagged

from ..models.api import MODEL_HINTS, REASONS, invalid
from .api_common import O3pMcpApiCase


@tagged("post_install", "-at_install")
class TestErrorDetails(O3pMcpApiCase):
    def search(self, user="restricted", **params):
        return self.call("search", {"model": "res.partner", "fields": ["name"], **params}, user=user)

    def test_reasons_and_fields(self):
        cases = [
            ({"fields": ["name", "nope"]}, "unknown_field", "nope"),
            ({"fields": "name"}, "bad_value", "fields"),
            ({"domain": [["category_id.nope", "=", 1]]}, "unknown_field", "category_id.nope"),
            ({"domain": [["category_id", "any", [["nope", "=", 1]]]]}, "unknown_field", "category_id.nope"),
            ({"domain": [["name.x", "=", 1]]}, "not_relational", "name"),
            ({"domain": [["name", "child_of", [1]]]}, "bad_operator", "name"),
            ({"domain": [["child_ids", "<", 5]]}, "bad_operator", "child_ids"),
            ({"domain": [["name", "=", {"a": 1}]]}, "bad_value", "name"),
            ({"domain": [["parent_id", "ilike", 3]]}, "bad_value", "parent_id"),
            ({"domain": "x"}, "bad_domain", "domain"),
            ({"domain": ["&"]}, "bad_domain", "domain"),
            ({"domain": [["parent_id.parent_id.parent_id.parent_id.name", "=", "x"]]}, "too_deep",
             "parent_id.parent_id.parent_id.parent_id.name"),
            ({"order": "name; drop"}, "bad_order", "order"),
            ({"order": "display_name"}, "not_sortable", "display_name"),
            ({"order": "nope desc"}, "unknown_field", "nope"),
            ({"limit": 999}, "bad_value", "limit"),
            ({"offset": -1}, "bad_value", "offset"),
            ({"having": []}, "unknown_param", "having"),
        ]
        nested = {"category_id.nope": "res.partner.category"}
        for params, reason, field in cases:
            with self.subTest(params=params):
                self.assertDetails(
                    self.search(**params), "invalid_params", reason, field, model=nested.get(field)
                )

    def test_field_access_reasons(self):
        with self.protected_field("res.partner", "function"):
            self.assertDetails(self.search(fields=["function"]), "invalid_params", "not_readable", "function")
            self.assertDetails(
                self.search(domain=[["function", "=", "x"]]), "invalid_params", "not_readable", "function"
            )
        self.assertDetails(
            self.call("search", {"model": "res.users", "fields": ["login"],
                                 "domain": [["partner_id", "ilike", "x"]]}),
            "invalid_params", "bypass_relation", "partner_id",
        )
        self.assertDetails(
            self.call("search", {"model": "res.users", "fields": ["login"],
                                 "domain": [["log_ids", "!=", False]]}),
            "access_denied", "no_relation_access", "log_ids",
        )
        self.assertDetails(
            self.call("describe", {"model": "ir.cron"}), "access_denied", "no_model_access", model="ir.cron"
        )

    def test_group_reasons(self):
        def group(**params):
            return self.call("group", {"model": "res.partner", "aggregates": ["__count"], **params})

        self.assertDetails(group(groupby=["create_date"]), "invalid_params", "granularity_required", "create_date")
        self.assertDetails(group(groupby=["name:month"]), "invalid_params", "bad_granularity", "name:month")
        self.assertDetails(group(groupby=["create_date:decade"]), "invalid_params", "bad_granularity",
                           "create_date:decade")
        self.assertDetails(group(groupby=["display_name"]), "invalid_params", "not_groupable", "display_name")
        self.assertDetails(group(groupby=["type"], aggregates=["name:sum"]), "invalid_params", "bad_aggregate",
                           "name:sum")
        self.assertDetails(group(groupby=["type"], order="color desc"), "invalid_params", "bad_order", "order")
        self.assertIn(":month", REASONS["granularity_required"])

    def test_details_hold_no_values(self):
        response = self.search(domain=[["name", "=", {"secret": "s3cr3t-value"}]])
        self.assertNotIn("s3cr3t", response.text)
        response = self.search(fields=["name", "x" * 300])
        self.assertNotIn("field", response.json()["error"]["details"])
        response = self.call("search", {"model": "res.partner", "fields": ["name"], "bad key!": 1})
        self.assertEqual(response.json()["error"]["details"]["reason"], "unknown_param")
        self.assertNotIn("field", response.json()["error"]["details"])

    def test_unknown_model_and_aggregate_hints(self):
        self.assertDetails(
            self.call("describe", {"model": "no.such.model"}), "unknown_model", "unknown_model",
            model="no.such.model",
        )
        details = self.assertDetails(
            self.call("search", {"model": "ir.config_parameter", "fields": ["key"]}, user="admin"),
            "unknown_model", "unknown_model", model="ir.config_parameter",
        )
        self.assertIn("odoo_find_models", details["hint"])
        self.assertIn("stored numeric", REASONS["bad_aggregate"])
        self.assertDetails(
            self.call("group", {"model": "res.partner", "groupby": ["type"],
                                "aggregates": ["active_lang_count:sum"]}, user="admin"),
            "invalid_params", "bad_aggregate", "active_lang_count:sum",
        )

    def test_model_hints(self):
        error = invalid("no_model_access", model="hr.employee", code="access_denied").to_dict()["error"]
        self.assertEqual(error["details"]["hint"], MODEL_HINTS["hr.employee"])
        self.assertIn("hr.employee.public", error["details"]["hint"])
        self.assertEqual(error["code"], "access_denied")


@tagged("post_install", "-at_install")
class TestNotSearchableFields(O3pMcpApiCase):
    """Odoo ignores conditions on computed fields it cannot search."""

    def test_unsearchable_fields_are_refused(self):
        def search(**params):
            return self.call("search", {"model": "res.partner", "fields": ["name"], **params}, user="admin")

        for domain, field, owner in (
            ([["active_lang_count", "=", 0]], "active_lang_count", None),
            ([["contact_address", "ilike", "x"]], "contact_address", None),
            ([["same_vat_partner_id.name", "=", "x"]], "same_vat_partner_id", None),
            ([["parent_id.active_lang_count", ">", 0]], "parent_id.active_lang_count", "res.partner"),
            ([["category_id", "any", [["partner_ids.contact_address", "=", "x"]]]],
             "category_id.partner_ids.contact_address", "res.partner"),
        ):
            with self.subTest(domain=domain):
                self.assertDetails(
                    search(domain=domain), "invalid_params", "not_searchable", field, model=owner
                )
        self.assertDetails(search(order="active_lang_count"), "invalid_params", "not_sortable", "active_lang_count")
        self.assertDetails(
            self.call("group", {"model": "res.partner", "groupby": ["active_lang_count"], "aggregates": ["__count"]},
                      user="admin"),
            "invalid_params", "not_groupable", "active_lang_count",
        )

    def test_searchable_computed_fields_still_work(self):
        names = [
            row["name"]
            for row in self.result(
                "search", {"model": "res.partner", "domain": [["display_name", "ilike", "MCPT Alpha"]],
                           "fields": ["name"]},
            )["records"]
        ]
        self.assertEqual(names, ["MCPT Alpha"])
        response = self.call(
            "search", {"model": "res.users", "domain": [["res_users_settings_id", "=", False]], "fields": ["login"]},
            user="admin",
        )
        self.assertEqual(response.status_code, 200, response.text)
