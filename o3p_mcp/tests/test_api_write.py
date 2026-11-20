from unittest.mock import patch

from odoo import Command, api
from odoo.tests import new_test_user, tagged

from ..models.api import ApiError
from .api_common import O3pMcpApiCase
from .common import RESOURCE


WRITE_SCOPES = {"okf.read", "odoo.read", "odoo.write"}


@tagged("post_install", "-at_install")
class TestApiWrite(O3pMcpApiCase):
    """create and update: as the user, on opened models, never unrecorded."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.record_group = env.ref("o3p_mcp.group_mcp_record_editor")
        cls.partner_manager = env.ref("base.group_partner_manager")
        (cls.restricted + cls.admin).write(
            {
                "groups_id": [
                    Command.link(cls.record_group.id),
                    Command.link(cls.partner_manager.id),
                ]
            }
        )
        # An MCP record editor without Odoo's right to change contacts.
        cls.reader_only = new_test_user(
            env,
            "o3p_mcp_reader_only",
            groups="base.group_user,o3p_mcp.group_mcp_user,o3p_mcp.group_mcp_record_editor",
            name="Reader Only",
        )
        cls.open_models("res.partner\nres.partner.category")
        Token = env["o3p.mcp.token"]
        cls.write_tokens = {
            name: Token._issue(cls.client, user, WRITE_SCOPES, RESOURCE)[1]["access_token"]
            for name, user in (
                ("admin", cls.admin),
                ("restricted", cls.restricted),
                ("editor", cls.editor),
                ("reader_only", cls.reader_only),
            )
        }

    @classmethod
    def open_models(cls, value):
        cls.env["ir.config_parameter"].sudo().set_param("o3p_mcp.api_writable_models", value)

    def write(self, op, params, user="restricted"):
        return self.call(op, params, token=self.write_tokens[user])

    def written(self, op, params, user="restricted"):
        response = self.write(op, params, user)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def new_partner(self, **values):
        return {
            "model": "res.partner",
            "records": [{"name": "MCPT Written", "category_id": [self.visible.id], **values}],
        }

    def count_written(self):
        return self.env["res.partner"].with_context(active_test=False).search_count(
            [("name", "=like", "MCPT Written%")]
        )

    def last_log(self):
        return self.env["o3p.mcp.api.log"].search([], order="id desc", limit=1)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def test_create_as_the_user_and_record_it(self):
        result = self.written(
            "create", self.new_partner(email="written@example.com", ref="R-1", type="invoice")
        )
        record = self.env["res.partner"].browse(result["records"][0]["id"])
        self.assertEqual(result, {"records": [{"id": record.id, "display_name": "MCPT Written"}]})
        self.assertEqual(record.create_uid, self.restricted)
        self.assertEqual(
            (record.email, record.ref, record.type, record.category_id),
            ("written@example.com", "R-1", "invoice", self.visible),
        )
        log = self.last_log()
        self.assertEqual(
            (log.op, log.model, log.status, log.row_count, log.user_id, log.record_ids, log.field_names),
            (
                "create",
                "res.partner",
                200,
                1,
                self.restricted,
                str(record.id),
                "category_id,email,name,ref,type",
            ),
        )
        # Several records in one call, all or nothing.
        two = self.written(
            "create",
            {
                "model": "res.partner",
                "records": [
                    {"name": "MCPT Written 2", "category_id": [self.visible.id]},
                    {"name": "MCPT Written 3", "category_id": [self.visible.id]},
                ],
            },
        )
        self.assertEqual(len(two["records"]), 2)
        self.assertEqual(self.last_log().row_count, 2)

    def test_nothing_is_writable_until_a_model_is_opened(self):
        self.open_models("")
        self.assertDetails(
            self.write("create", self.new_partner()), "access_denied", "model_not_writable", model="res.partner"
        )
        self.assertEqual(self.count_written(), 0)
        self.open_models("res.partner.category")
        self.assertDetails(
            self.write("create", self.new_partner()), "access_denied", "model_not_writable", model="res.partner"
        )
        log = self.last_log()
        self.assertEqual((log.op, log.status, log.error, log.record_ids), ("create", 403, "access_denied", False))

    def test_the_scope_and_odoo_rights_are_required(self):
        # A token without odoo.write, and a user without MCP records / Editor.
        self.assertApiError(self.call("create", self.new_partner()), "insufficient_scope")
        self.assertApiError(self.write("create", self.new_partner(), user="editor"), "insufficient_scope")
        # The MCP right is not Odoo's right to change contacts.
        self.assertDetails(
            self.write("create", self.new_partner(), user="reader_only"),
            "access_denied",
            "no_write_access",
            model="res.partner",
        )
        self.assertApiError(
            self.write(
                "update",
                {"model": "res.partner", "ids": [self.alpha.id], "values": {"email": "x@example.com"}},
                user="reader_only",
            ),
            "access_denied",
        )
        self.assertFalse(self.alpha.email)
        # The scope follows the groups at every call.
        self.restricted.write({"groups_id": [Command.unlink(self.record_group.id)]})
        self.assertApiError(self.write("create", self.new_partner()), "insufficient_scope")
        self.assertEqual(self.count_written(), 0)

    def test_models_that_grant_access_or_hold_code_are_never_writable(self):
        self.open_models("*")
        for model in ("res.users", "res.groups", "mail.message", "mail.mail"):
            with self.subTest(model=model):
                response = self.write(
                    "create", {"model": model, "records": [{"name": "MCPT Written"}]}, user="admin"
                )
                self.assertDetails(response, "access_denied", "model_not_writable", model=model)
        # Hidden models stay unknown.
        self.assertApiError(
            self.write(
                "create",
                {"model": "ir.config_parameter", "records": [{"key": "x", "value": "y"}]},
                user="admin",
            ),
            "unknown_model",
        )
        # Any other model is opened by the pattern.
        result = self.written(
            "create", {"model": "res.partner.category", "records": [{"name": "MCPT Written tag"}]}, user="admin"
        )
        self.assertEqual(result["records"][0]["display_name"], "MCPT Written tag")

    def test_fields_that_cannot_be_written(self):
        Partner = self.env["res.partner"]
        related = next(
            (name for name, field in Partner._fields.items() if field.related and not field.readonly and field.store),
            None,
        )
        cases = {
            "nope": "unknown_field",
            "id": "not_writable",
            "display_name": "not_writable",
            "create_date": "not_writable",
            "commercial_partner_id": "not_writable",
            "image_1920": "not_writable",
            "child_ids": "not_writable",
        }
        if related:
            cases[related] = "not_writable"
        for name, reason in cases.items():
            with self.subTest(field=name):
                self.assertDetails(
                    self.write("create", self.new_partner(**{name: False})), "invalid_params", reason, field=name
                )
        with self.protected_field("res.partner", "function"):
            self.assertDetails(
                self.write("create", self.new_partner(function="Boss")), "invalid_params", "not_writable", field="function"
            )
        self.assertEqual(self.count_written(), 0)

    def test_values_are_checked(self):
        for name, value in (
            ("name", 5),
            ("name", "x" * 100_001),
            ("color", "3"),
            ("color", True),
            ("active", "yes"),
            ("type", "nope"),
            ("parent_id", "MCPT Alpha"),
            ("parent_id", [self.alpha.id]),
            ("category_id", self.visible.id),
            ("category_id", ["x"]),
            ("category_id", {"add": [self.visible.id], "set": []}),
            ("category_id", {}),
        ):
            with self.subTest(field=name, value=value):
                self.assertDetails(
                    self.write("create", self.new_partner(**{name: value})), "invalid_params", "bad_value", field=name
                )
        for params in (
            {"model": "res.partner"},
            {"model": "res.partner", "records": []},
            {"model": "res.partner", "records": [{"name": "MCPT Written"}] * 21},
            {"model": "res.partner", "records": {"name": "MCPT Written"}},
            {"model": "res.partner", "records": [{}]},
            {"model": "res.partner", "records": ["MCPT Written"]},
            {**self.new_partner(), "ids": [1]},
        ):
            with self.subTest(params=params):
                self.assertApiError(self.write("create", params), "invalid_params")
        self.assertEqual(self.count_written(), 0)
        # false empties a field; a parent is given by id.
        result = self.written("create", self.new_partner(parent_id=self.alpha.id, email=False))
        record = self.env["res.partner"].browse(result["records"][0]["id"])
        self.assertEqual((record.parent_id, record.email), (self.alpha, False))

    def test_related_records_must_be_readable(self):
        # A record the user may not read and one that does not exist answer alike.
        hidden = self.write("create", self.new_partner(parent_id=self.hidden.id))
        missing = self.write("create", self.new_partner(parent_id=self.hidden.id + 100_000))
        self.assertDetails(hidden, "invalid_params", "unknown_record", field="parent_id")
        self.assertEqual(hidden.json(), missing.json())
        self.assertDetails(
            self.write("create", self.new_partner(category_id=[self.visible.id, 999_999_999])),
            "invalid_params",
            "unknown_record",
            field="category_id",
        )
        self.assertEqual(self.count_written(), 0)

    def test_required_fields_and_odoo_messages(self):
        self.assertDetails(
            self.write("create", {"model": "res.partner.category", "records": [{"color": 2}]}),
            "invalid_params",
            "required_missing",
            field="name",
        )
        # A constraint of Odoo answers with Odoo's own message for the user.
        tag = self.env["res.partner.category"].create({"name": "MCPT Written tag"})
        response = self.write(
            "update", {"model": "res.partner.category", "ids": [tag.id], "values": {"parent_id": tag.id}}
        )
        self.assertApiError(response, "query_error")
        error = response.json()["error"]
        self.assertEqual(list(error["details"]), ["message"])
        self.assertRegex(error["details"]["message"].lower(), "recurs|loop")
        self.assertFalse(tag.parent_id)
        log = self.last_log()
        self.assertEqual((log.status, log.error, log.record_ids), (422, "query_error", False))

    def test_a_failed_write_queues_nothing_for_after_the_commit(self):
        api = self.env["o3p.mcp.api"].with_user(self.restricted)
        cr = self.env.cr
        before = len(cr.postcommit._funcs)
        with self.assertRaises(ApiError) as raised:
            api._run("create", {"model": "res.partner.category", "records": [{"color": 2}]})
        self.assertEqual(raised.exception.code, "invalid_params")
        tag = self.env["res.partner.category"].create({"name": "MCPT Written tag"})
        with self.assertRaises(ApiError) as raised:
            api._run(
                "update", {"model": "res.partner.category", "ids": [tag.id], "values": {"parent_id": tag.id}}
            )
        self.assertEqual(raised.exception.code, "query_error")
        self.assertEqual(len(cr.postcommit._funcs), before)

    def test_no_write_commits_without_its_log_row(self):
        Log = type(self.env["o3p.mcp.api.log"])
        original = Log.create

        @api.model_create_multi
        def failing(records, vals_list):
            if any(vals.get("status") == 200 for vals in vals_list):
                raise RuntimeError("the call log is not available")
            return original(records, vals_list)

        with patch.object(Log, "create", failing):
            response = self.write("create", self.new_partner())
        self.assertApiError(response, "query_error")
        self.assertEqual(self.count_written(), 0)
        log = self.last_log()
        self.assertEqual((log.op, log.status, log.record_ids), ("create", 422, False))

    def test_company(self):
        company = self.restricted.company_id
        other = self.env["res.company"].create({"name": "MCPT Other Company"})
        for value in (other.id, "1", 0, True):
            with self.subTest(company=value):
                self.assertDetails(
                    self.write("create", {**self.new_partner(), "company": value}),
                    "invalid_params",
                    "bad_company",
                    field="company",
                )
        self.assertEqual(self.count_written(), 0)
        self.written("create", {**self.new_partner(), "company": company.id})
        self.assertEqual(self.count_written(), 1)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, ids, values, user="restricted"):
        return self.write("update", {"model": "res.partner", "ids": ids, "values": values}, user=user)

    def test_update_as_the_user_and_record_it(self):
        other = self.env["res.partner.category"].create({"name": "MCPT Written tag"})
        result = self.written(
            "update",
            {
                "model": "res.partner",
                "ids": [self.alpha.id, self.beta.id],
                "values": {"email": "changed@example.com", "category_id": {"add": [other.id]}},
            },
        )
        self.assertEqual(
            result,
            {
                "records": [
                    {"id": self.alpha.id, "display_name": "MCPT Alpha"},
                    {"id": self.beta.id, "display_name": "MCPT Beta"},
                ]
            },
        )
        self.assertEqual((self.alpha + self.beta).mapped("email"), ["changed@example.com"] * 2)
        self.assertEqual(self.alpha.category_id, self.visible + other)
        self.assertEqual(self.alpha.write_uid, self.restricted)
        log = self.last_log()
        self.assertEqual(
            (log.op, log.status, log.row_count, log.record_ids, log.field_names),
            ("update", 200, 2, f"{self.alpha.id},{self.beta.id}", "category_id,email"),
        )
        self.written(
            "update",
            {"model": "res.partner", "ids": [self.alpha.id], "values": {"category_id": {"remove": [other.id]}}},
        )
        self.assertEqual(self.alpha.category_id, self.visible)
        self.written(
            "update", {"model": "res.partner", "ids": [self.alpha.id], "values": {"category_id": [other.id, self.visible.id]}}
        )
        self.assertEqual(self.alpha.category_id, self.visible + other)

    def test_update_of_hidden_and_missing_records_answers_alike(self):
        hidden = self.update([self.hidden.id], {"email": "x@example.com"})
        missing = self.update([self.hidden.id + 100_000], {"email": "x@example.com"})
        self.assertDetails(hidden, "invalid_params", "unknown_record", field="ids")
        self.assertEqual(hidden.json(), missing.json())
        # One hidden record refuses the whole call.
        self.assertDetails(
            self.update([self.alpha.id, self.hidden.id], {"email": "x@example.com"}),
            "invalid_params",
            "unknown_record",
            field="ids",
        )
        self.assertFalse(self.alpha.email)
        self.assertFalse(self.hidden.email)
        for ids in ([], "1", [0], ["1"], list(range(1, 102)), None):
            with self.subTest(ids=ids):
                self.assertDetails(self.update(ids, {"email": "x@example.com"}), "invalid_params", "bad_value", field="ids")
        self.assertApiError(self.update([self.alpha.id], {}), "invalid_params")

    def test_update_respects_record_rules_and_is_all_or_nothing(self):
        self.env["ir.rule"].create(
            {
                "name": "o3p_mcp test: the restricted user does not change Beta",
                "model_id": self.env.ref("base.model_res_partner").id,
                "perm_read": False,
                "perm_write": True,
                "perm_create": False,
                "perm_unlink": False,
                "domain_force": f"[('id', '!=', {self.beta.id})] if user.id == {self.restricted.id} else []",
            }
        )
        response = self.update([self.alpha.id, self.beta.id], {"email": "x@example.com"})
        self.assertDetails(response, "access_denied", "no_record_access")
        self.assertFalse(self.alpha.email)
        self.assertFalse(self.beta.email)
        self.assertEqual(self.last_log().status, 403)
        # The administrator is not under that rule.
        self.written(
            "update",
            {"model": "res.partner", "ids": [self.beta.id], "values": {"email": "admin@example.com"}},
            user="admin",
        )
        self.assertEqual(self.beta.email, "admin@example.com")

    # ------------------------------------------------------------------
    # Describe
    # ------------------------------------------------------------------

    def test_describe_tells_what_can_be_written(self):
        result = self.result("describe", {"model": "res.partner"})
        self.assertEqual(result["access"], {"create": True, "write": True})
        writable = {item["name"]: item["writable"] for item in result["fields"]}
        self.assertEqual(
            {name: writable[name] for name in ("name", "email", "parent_id", "category_id", "child_ids", "display_name", "commercial_partner_id")},
            {
                "name": True,
                "email": True,
                "parent_id": True,
                "category_id": True,
                "child_ids": False,
                "display_name": False,
                "commercial_partner_id": False,
            },
        )
        # Odoo's rights decide: on Odoo 18 an internal user without the
        # contact creation group may neither create nor change contacts.
        self.assertEqual(
            self.result("describe", {"model": "res.partner"}, user="editor")["access"],
            {"create": False, "write": False},
        )
        users = self.result("describe", {"model": "res.users"}, user="admin")
        self.assertEqual(users["access"], {"create": False, "write": False})
        self.assertFalse(any(item["writable"] for item in users["fields"]))
