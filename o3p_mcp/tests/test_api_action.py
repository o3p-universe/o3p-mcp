from unittest.mock import patch

from odoo import Command, api
from odoo.exceptions import ValidationError
from odoo.tests import Form, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.api_action import MSG_COMMITS
from ..models.mcp_action import WIZARD_NEVER
from .api_common import READ_SCOPES, O3pMcpApiCase
from .common import RESOURCE


WRITE_SCOPES = {"okf.read", "odoo.read", "odoo.write"}
MERGE = "base.partner.merge.automatic.wizard"
# Wizards that may be exposed (wizard: what its buttons do), reviewed on a
# standard Odoo 18 and 20 install. Entries for modules that are not installed
# are kept. Every other wizard must be refused by WIZARD_NEVER, by the list
# of models the API never writes or by the list of hidden models.
REVIEWED_WIZARDS = {
    "base.partner.merge.automatic.wizard": "merges contacts the user may change",
    "base.partner.merge.line": "a line of the merge wizard",
    "calendar.event.archive.or.unlink.wizard": "archives or deletes the user's own event",
    "calendar.event.multi.archive.or.unlink.wizard": "archives or deletes the user's own events",
    "calendar.popover.delete.wizard": "deletes a calendar event the user may delete",
    "project.project.stage.delete.wizard": "archives or deletes project stages",
    "project.task.type.delete.wizard": "archives or deletes task stages",
    "project.template.create.wizard": "creates a project from a template",
    "project.template.role.to.users.map": "a line of the project template wizard",
    "sale.order.discount": "applies a discount to a quotation the user may change",
}


@tagged("post_install", "-at_install")
class TestApiAction(O3pMcpApiCase):
    """Exposed actions: registered one by one, listed for those who may run
    them, run as the user and never unrecorded."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.record_group = env.ref("o3p_mcp.group_mcp_record_editor")
        cls.partner_manager = env.ref("base.group_partner_manager")
        (cls.restricted + cls.admin).write(
            {
                "group_ids": [
                    Command.link(cls.record_group.id),
                    Command.link(cls.partner_manager.id),
                ]
            }
        )
        # An MCP record editor without Odoo's right to change contact tags.
        cls.reader_only = new_test_user(
            env,
            "o3p_mcp_action_reader",
            groups="base.group_user,o3p_mcp.group_mcp_user,o3p_mcp.group_mcp_record_editor",
            name="Reader Only",
        )
        cls.tokens["reader_only"] = env["o3p.mcp.token"]._issue(
            cls.client, cls.reader_only, READ_SCOPES, RESOURCE
        )[1]["access_token"]
        cls.write_tokens = {
            name: env["o3p.mcp.token"]._issue(cls.client, user, WRITE_SCOPES, RESOURCE)[1]["access_token"]
            for name, user in (
                ("admin", cls.admin),
                ("restricted", cls.restricted),
                ("editor", cls.editor),
                ("reader_only", cls.reader_only),
            )
        }
        cls.partner_model = env.ref("base.model_res_partner")
        cls.Action = env["o3p.mcp.action"]
        cls.open_company = cls.Action.create(
            cls.button(name="open-company", title="Open the company of a contact")
        )
        cls.server_action = env["ir.actions.server"].create(
            {
                "name": "MCPT mark as reviewed",
                "model_id": cls.partner_model.id,
                "state": "code",
                "code": "records.write({'ref': 'MCPT-REVIEWED'})",
            }
        )
        cls.mark_reviewed = cls.Action.create(
            {
                "name": "mark-reviewed",
                "title": "Mark contacts as reviewed",
                "description": "Sets the reference of the contacts to MCPT-REVIEWED.",
                "kind": "server",
                "server_action_id": cls.server_action.id,
                "max_records": 5,
            }
        )
        cls.recolor_tags = cls.Action.create(
            {
                "name": "recolor-tags",
                "title": "Recolor contact tags",
                "description": "Gives the contact tags the color 5.",
                "kind": "server",
                "server_action_id": env["ir.actions.server"]
                .create(
                    {
                        "name": "MCPT recolor",
                        "model_id": env.ref("base.model_res_partner_category").id,
                        "state": "code",
                        "code": "records.write({'color': 5})",
                    }
                )
                .id,
                "max_records": 100,
            }
        )

    @classmethod
    def button(cls, **values):
        return {
            "name": "a-button",
            "title": "A button",
            "description": "Opens the company a contact belongs to.",
            "kind": "method",
            "model_id": cls.partner_model.id,
            "method": "open_commercial_entity",
            **values,
        }

    def names(self, user="restricted", **params):
        return [entry["name"] for entry in self.result("actions", params, user=user)["actions"]]

    def run_action(self, name, ids, user="restricted", **params):
        return self.call(
            "run_action", {"action": name, "ids": ids, **params}, token=self.write_tokens[user]
        )

    def ran(self, name, ids, user="restricted", **params):
        response = self.run_action(name, ids, user, **params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def last_log(self):
        return self.env["o3p.mcp.api.log"].search([], order="id desc", limit=1)

    def wizard(self, **values):
        """Expose the contact merge wizard of Odoo's base module."""
        return self.Action.create(
            {
                "name": "merge-contacts",
                "title": "Merge contacts",
                "description": "Merges two or three contacts into the destination contact.",
                "kind": "wizard",
                "model_id": self.partner_model.id,
                "min_records": 2,
                "max_records": 3,
                "wizard_model_id": self.env["ir.model"]._get(MERGE).id,
                "steps": "action_merge",
                "inputs": "dst_partner_id",
                **values,
            }
        )

    def twins(self):
        """Two contacts the restricted user may merge."""
        return self.env["res.partner"].create(
            [
                {"name": f"MCPT Twin {letter}", "category_id": [Command.link(self.visible.id)]}
                for letter in "AB"
            ]
        )

    def code_action(self, name, code):
        """Expose a server action on contacts that runs ``code``."""
        server_action = self.env["ir.actions.server"].create(
            {"name": f"MCPT {name}", "model_id": self.partner_model.id, "state": "code", "code": code}
        )
        return self.Action.create(
            {
                "name": name,
                "title": name,
                "description": "A test action.",
                "kind": "server",
                "server_action_id": server_action.id,
            }
        )

    # ------------------------------------------------------------------
    # The registry
    # ------------------------------------------------------------------

    def test_what_cannot_be_exposed(self):
        model = lambda xmlid: self.env.ref(xmlid).id  # noqa: E731
        refused = {
            "a name that is not a slug": {"name": "Open Company"},
            "a private method": {"method": "_compute_display_name"},
            "a method every model has: unlink": {"method": "unlink"},
            "a method every model has: write": {"method": "write"},
            "a method every model has: copy": {"method": "copy"},
            "a method that does not exist": {"method": "no_such_button"},
            "something that is not a method name": {"method": "open_commercial_entity()"},
            "a model that grants access": {
                "model_id": model("base.model_res_users"),
                "method": "action_reset_password",
            },
            "a hidden model": {
                "model_id": model("base.model_ir_config_parameter"),
                "method": "get_str",
            },
            "more records than the fewest": {"min_records": 2, "max_records": 1},
            "more than 100 records": {"max_records": 101},
            "a button without a record": {"min_records": 0},
        }
        for case, values in refused.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                self.Action.create(self.button(**values))

    def test_server_actions_that_cannot_be_exposed(self):
        ServerAction = self.env["ir.actions.server"]
        users = self.env.ref("base.model_res_users")
        refused = {
            "on a model that grants access": ServerAction.create(
                {"name": "MCPT on users", "model_id": users.id, "state": "code", "code": "pass"}
            ),
            "the code of a scheduled job": ServerAction.create(
                {
                    "name": "MCPT job",
                    "model_id": self.partner_model.id,
                    "state": "code",
                    "code": "pass",
                    "usage": "ir_cron",
                }
            ),
            "creating records of a model that grants access": ServerAction.create(
                {
                    "name": "MCPT create a user",
                    "model_id": self.partner_model.id,
                    "state": "object_create",
                    "crud_model_id": users.id,
                    "value": "MCPT",
                }
            ),
        }
        for case, server_action in refused.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                self.Action.create(
                    {
                        "name": "refused",
                        "title": "Refused",
                        "description": "Cannot be exposed.",
                        "kind": "server",
                        "server_action_id": server_action.id,
                    }
                )

    def test_the_records_are_those_of_the_server_action(self):
        self.assertEqual(self.mark_reviewed.model_id, self.partner_model)
        self.assertEqual(
            self.mark_reviewed.summary,
            "Runs the server action MCPT mark as reviewed on 1 to 5 records of Contact (res.partner).",
        )
        self.assertEqual(
            self.open_company.summary,
            "Presses the button open_commercial_entity on one record of Contact (res.partner).",
        )
        self.assertFalse(self.open_company.problem)

    def test_the_form_tells_what_runs(self):
        with Form(self.Action) as form:
            form.title = "Mark as reviewed, from the form"
            form.name = "mark-from-form"
            form.description = "Sets the reference."
            form.kind = "server"
            form.server_action_id = self.server_action
            self.assertEqual(form.model_id, self.partner_model)
            self.assertIn("MCPT mark as reviewed", form.summary)
        self.assertEqual(form.record.model_id, self.partner_model)
        settings = self.env["res.config.settings"].create({})
        self.assertEqual(settings.o3p_mcp_action_count, 4)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def test_actions_lists_what_the_user_may_run(self):
        result = self.result("actions", {"model": "res.partner"})
        self.assertEqual(
            result,
            {
                "actions": [
                    {
                        "name": "mark-reviewed",
                        "title": "Mark contacts as reviewed",
                        "description": "Sets the reference of the contacts to MCPT-REVIEWED.",
                        "kind": "server",
                        "model": "res.partner",
                        "records": [1, 5],
                        "inputs": [],
                    },
                    {
                        "name": "open-company",
                        "title": "Open the company of a contact",
                        "description": "Opens the company a contact belongs to.",
                        "kind": "method",
                        "model": "res.partner",
                        "records": [1, 1],
                        "inputs": [],
                    },
                ],
                "total": 2,
                "truncated": False,
            },
        )
        everything = ["mark-reviewed", "open-company", "recolor-tags"]
        self.assertEqual(self.names(), everything)
        # Odoo does not let this user change contact tags.
        self.assertNotIn("recolor-tags", self.names(user="reader_only"))
        # Limited to a group the user does not hold.
        self.mark_reviewed.group_ids = self.env.ref("base.group_system")
        self.assertEqual(self.names(), ["open-company", "recolor-tags"])
        self.assertEqual(self.names(user="admin"), everything)
        # A server action Odoo itself limits to a group.
        self.mark_reviewed.group_ids = False
        self.server_action.group_ids = self.env.ref("base.group_system")
        self.assertEqual(self.names(), ["open-company", "recolor-tags"])
        self.server_action.group_ids = False
        # Archived.
        self.open_company.active = False
        self.assertEqual(self.names(), ["mark-reviewed", "recolor-tags"])

    def test_actions_are_searched_and_paged(self):
        self.assertEqual(self.names(query="REVIEWED"), ["mark-reviewed"])
        self.assertEqual(self.names(query="company a contact belongs"), ["open-company"])
        self.assertEqual(
            self.names(query="res.partner"), ["mark-reviewed", "open-company", "recolor-tags"]
        )
        self.assertEqual(self.names(model="res.partner"), ["mark-reviewed", "open-company"])
        self.assertEqual(self.names(model="res.partner.category"), ["recolor-tags"])
        self.assertEqual(self.names(model="res.partner.title"), [])
        page = self.result("actions", {"limit": 1, "offset": 2})
        self.assertEqual([entry["name"] for entry in page["actions"]], ["recolor-tags"])
        self.assertEqual((page["total"], page["truncated"]), (3, False))
        self.assertTrue(self.result("actions", {"limit": 1})["truncated"])
        for params, field in (
            ({"limit": 0}, "limit"),
            ({"limit": 51}, "limit"),
            ({"query": 3}, "query"),
            ({"model": ["res.partner"]}, "model"),
            ({"offset": -1}, "offset"),
        ):
            with self.subTest(params=params):
                self.assertDetails(self.call("actions", params), "invalid_params", "bad_value", field)
        self.assertDetails(
            self.call("actions", {"kind": "method"}), "invalid_params", "unknown_param", "kind"
        )

    def test_describe_action(self):
        self.assertEqual(
            self.result("describe_action", {"action": "open-company"}),
            {
                "name": "open-company",
                "title": "Open the company of a contact",
                "description": "Opens the company a contact belongs to.",
                "kind": "method",
                "model": "res.partner",
                "records": [1, 1],
                "inputs": [],
                "summary": "Presses the button open_commercial_entity on one record of Contact (res.partner).",
                "steps": ["open_commercial_entity"],
            },
        )
        self.assertEqual(self.result("describe_action", {"action": "mark-reviewed"})["steps"], [])
        log = self.env["o3p.mcp.api.log"].search([], order="id desc", limit=1)
        self.assertEqual((log.op, log.model, log.status), ("describe_action", "res.partner", 200))

    def test_an_action_for_others_answers_like_one_that_does_not_exist(self):
        self.mark_reviewed.group_ids = self.env.ref("base.group_system")
        hidden = self.call("describe_action", {"action": "mark-reviewed"})
        missing = self.call("describe_action", {"action": "no-such-action"})
        self.assertDetails(hidden, "invalid_params", "unknown_action", "action")
        self.assertEqual(hidden.json(), missing.json())
        for name in (None, 7, "Not A Slug", "x" * 65):
            with self.subTest(name=name):
                self.assertApiError(self.call("describe_action", {"action": name}), "invalid_params")
        # Odoo's own rights are named.
        self.assertDetails(
            self.call("describe_action", {"action": "recolor-tags"}, user="reader_only"),
            "access_denied",
            "no_write_access",
            model="res.partner.category",
        )

    def test_an_action_whose_method_is_gone_is_unavailable(self):
        self.env.cr.execute(
            "UPDATE o3p_mcp_action SET method = 'removed_by_an_upgrade' WHERE id = %s",
            [self.open_company.id],
        )
        self.open_company.invalidate_recordset()
        self.assertEqual(self.open_company.problem, "res.partner has no method removed_by_an_upgrade.")
        self.assertFalse(self.open_company.summary)
        self.assertEqual(self.names(), ["mark-reviewed", "recolor-tags"])
        self.assertDetails(
            self.call("describe_action", {"action": "open-company"}),
            "invalid_params",
            "action_unavailable",
            "action",
        )

    @mute_logger("odoo.http")
    def test_listing_needs_the_read_scope(self):
        token = self.env["o3p.mcp.token"]._issue(
            self.client, self.restricted, {"okf.read"}, RESOURCE
        )[1]["access_token"]
        for op, params in (("actions", {}), ("describe_action", {"action": "open-company"})):
            with self.subTest(op=op):
                self.assertApiError(self.call(op, params, token=token), "insufficient_scope")

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def test_run_a_button_as_the_user_and_record_it(self):
        result = self.ran("open-company", [self.alpha.id])
        self.assertEqual(
            result,
            {
                "done": True,
                "records": [{"id": self.alpha.id, "display_name": "MCPT Alpha"}],
                "returned": {
                    "type": "ir.actions.act_window",
                    "model": "res.partner",
                    "ids": [self.alpha.id],
                },
            },
        )
        log = self.last_log()
        self.assertEqual(
            (log.op, log.action, log.model, log.record_ids, log.field_names, log.status, log.row_count),
            ("run_action", "open-company", "res.partner", str(self.alpha.id), False, 200, 1),
        )
        self.assertEqual(log.user_id, self.restricted)

    def test_run_a_server_action_on_several_records(self):
        result = self.ran("mark-reviewed", [self.alpha.id, self.beta.id, self.alpha.id])
        self.assertEqual(
            result,
            {
                "done": True,
                "records": [
                    {"id": self.alpha.id, "display_name": "MCPT Alpha"},
                    {"id": self.beta.id, "display_name": "MCPT Beta"},
                ],
            },
        )
        self.assertEqual((self.alpha.ref, self.beta.ref), ("MCPT-REVIEWED", "MCPT-REVIEWED"))
        self.assertEqual(self.alpha.write_uid, self.restricted)
        log = self.last_log()
        self.assertEqual(
            (log.op, log.action, log.record_ids, log.row_count),
            ("run_action", "mark-reviewed", f"{self.alpha.id},{self.beta.id}", 2),
        )

    @mute_logger("odoo.http")
    def test_the_scope_the_group_and_odoo_rights_are_required(self):
        ids = [self.alpha.id]
        color = self.visible.color
        # A token without odoo.write, and a user without MCP records / Editor.
        self.assertApiError(
            self.call("run_action", {"action": "mark-reviewed", "ids": ids}), "insufficient_scope"
        )
        self.assertApiError(self.run_action("mark-reviewed", ids, user="editor"), "insufficient_scope")
        # Odoo does not let this user change the contact, or any contact tag.
        self.assertApiError(self.run_action("mark-reviewed", ids, user="reader_only"), "access_denied")
        self.assertDetails(
            self.run_action("recolor-tags", [self.visible.id], user="reader_only"),
            "access_denied",
            "no_write_access",
            model="res.partner.category",
        )
        # An action exposed to other groups answers like one that does not exist.
        self.mark_reviewed.group_ids = self.env.ref("base.group_system")
        hidden = self.run_action("mark-reviewed", ids)
        self.assertDetails(hidden, "invalid_params", "unknown_action", "action")
        self.assertEqual(hidden.json(), self.run_action("no-such-action", ids).json())
        self.assertEqual(self.ran("mark-reviewed", ids, user="admin")["done"], True)
        self.alpha.ref = False
        # A server action Odoo limits to a group is refused by Odoo's rights.
        self.mark_reviewed.group_ids = False
        self.server_action.group_ids = self.env.ref("base.group_system")
        self.assertApiError(self.run_action("mark-reviewed", ids), "access_denied")
        self.assertFalse(self.alpha.ref)
        self.assertEqual(self.visible.color, color)

    def test_records_are_checked(self):
        # A record the user may not read answers like one that does not exist.
        hidden = self.run_action("mark-reviewed", [self.alpha.id, self.hidden.id])
        self.assertDetails(hidden, "invalid_params", "unknown_record", "ids")
        self.assertEqual(
            hidden.json(), self.run_action("mark-reviewed", [self.alpha.id, 999_999_999]).json()
        )
        for name, ids in (
            ("open-company", [self.alpha.id, self.beta.id]),
            ("open-company", []),
            ("mark-reviewed", []),
            ("mark-reviewed", list(range(1, 7))),
        ):
            with self.subTest(name=name, ids=ids):
                self.assertDetails(
                    self.run_action(name, ids), "invalid_params", "record_count", "ids"
                )
        for ids in ("1", [0], [-1], [True], [1.5], {"id": 1}):
            with self.subTest(ids=ids):
                self.assertDetails(
                    self.run_action("mark-reviewed", ids), "invalid_params", "bad_value", "ids"
                )
        self.assertDetails(
            self.run_action("mark-reviewed", [self.alpha.id], domain=[]),
            "invalid_params",
            "unknown_param",
            "domain",
        )
        self.assertFalse(self.alpha.ref)
        # A failed run is recorded with the action's name and no records.
        self.run_action("mark-reviewed", [self.hidden.id])
        log = self.last_log()
        self.assertEqual(
            (log.op, log.action, log.status, log.error, log.record_ids),
            ("run_action", "mark-reviewed", 400, "invalid_params", False),
        )

    def test_odoo_refuses_and_its_message_comes_back(self):
        self.code_action(
            "refuse",
            "records.write({'ref': 'MCPT-NOT-KEPT'})\n"
            "raise UserError('MCPT: contacts are not reviewed on Sundays.')",
        )
        response = self.run_action("refuse", [self.alpha.id])
        self.assertApiError(response, "query_error")
        self.assertEqual(
            response.json()["error"]["details"],
            {"message": "MCPT: contacts are not reviewed on Sundays."},
        )
        self.assertFalse(self.alpha.ref)

    def test_an_action_that_commits_is_refused(self):
        self.code_action("commits", "records.write({'ref': 'MCPT-COMMITTED'})\nenv.cr.commit()")
        self.code_action("rolls-back", "records.write({'ref': 'MCPT-ROLLED'})\nenv.cr.rollback()")
        for name in ("commits", "rolls-back"):
            with self.subTest(name=name):
                response = self.run_action(name, [self.alpha.id])
                self.assertApiError(response, "query_error")
                self.assertEqual(response.json()["error"]["details"], {"message": MSG_COMMITS})
                self.assertFalse(self.alpha.ref)
        # The cursor commits again afterwards: the next run goes through.
        self.ran("mark-reviewed", [self.alpha.id])
        self.assertEqual(self.alpha.ref, "MCPT-REVIEWED")

    def test_no_run_commits_without_its_log_row(self):
        Log = type(self.env["o3p.mcp.api.log"])
        original = Log.create

        @api.model_create_multi
        def failing(records, vals_list):
            if any(vals.get("status") == 200 for vals in vals_list):
                raise RuntimeError("the call log is not available")
            return original(records, vals_list)

        with patch.object(Log, "create", failing):
            response = self.run_action("mark-reviewed", [self.alpha.id])
        self.assertApiError(response, "query_error")
        self.assertFalse(self.alpha.ref)
        log = self.last_log()
        self.assertEqual(
            (log.op, log.action, log.status, log.record_ids), ("run_action", "mark-reviewed", 422, False)
        )

    def test_what_a_button_answers_with_is_described(self):
        data_api = self.env["o3p.mcp.api"].with_user(self.restricted)
        cases = (
            (None, None),
            (True, None),
            ({"type": "ir.actions.act_window_close"}, None),
            ({"res_model": "res.partner"}, None),
            (
                {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {"title": "Started", "message": "It runs in the background.", "sticky": True},
                },
                {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "title": "Started",
                    "message": "It runs in the background.",
                },
            ),
            (
                {"type": "ir.actions.client", "tag": "reload", "params": {"message": "not a notification"}},
                {"type": "ir.actions.client", "tag": "reload"},
            ),
            (
                {"type": "ir.actions.act_window", "name": "Merge", "res_model": "base.partner.merge.automatic.wizard"},
                {
                    "type": "ir.actions.act_window",
                    "name": "Merge",
                    "model": "base.partner.merge.automatic.wizard",
                    "wizard": True,
                },
            ),
            # A hidden model is not named, a record the user may not read not given.
            (
                {"type": "ir.actions.act_window", "res_model": "ir.config_parameter", "res_id": 1},
                {"type": "ir.actions.act_window"},
            ),
            (
                {"type": "ir.actions.act_window", "res_model": "res.partner", "res_id": self.hidden.id},
                {"type": "ir.actions.act_window", "model": "res.partner", "ids": []},
            ),
            (
                {"type": "ir.actions.act_url", "url": "https://example.com/?token=secret"},
                {"type": "ir.actions.act_url"},
            ),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(data_api._returned(value), expected)

    # ------------------------------------------------------------------
    # Wizards
    # ------------------------------------------------------------------

    def test_run_a_wizard_as_the_user_and_record_it(self):
        self.wizard()
        first, second = self.twins()
        wizards = self.env[MERGE].search_count([])
        result = self.ran("merge-contacts", [first.id, second.id], values={"dst_partner_id": first.id})
        self.assertFalse(second.exists())
        self.assertEqual(result["done"], True)
        self.assertEqual(result["records"], [{"id": first.id, "display_name": "MCPT Twin A"}])
        # What the wizard shows after its button, and that it stays open.
        self.assertEqual(result["form"]["state"], "finished")
        self.assertEqual(result["form"]["dst_partner_id"], [first.id, "MCPT Twin A"])
        # The wizard stays open on its last page; with mail installed Odoo
        # answers with a client action that refreshes the contacts' threads.
        returned = result["returned"]
        if returned["type"] == "ir.actions.act_window":
            self.assertEqual(
                returned, {"type": "ir.actions.act_window", "model": MERGE, "wizard": True}
            )
        else:
            self.assertEqual(set(returned), {"type", "tag"})
            self.assertEqual(returned["type"], "ir.actions.client")
        log = self.last_log()
        self.assertEqual(
            (log.op, log.action, log.model, log.record_ids, log.field_names, log.status, log.row_count),
            ("run_action", "merge-contacts", "res.partner", f"{first.id},{second.id}", "dst_partner_id", 200, 2),
        )
        wizard = self.env[MERGE].search([], order="id desc", limit=1)
        self.assertEqual(self.env[MERGE].search_count([]), wizards + 1)
        self.assertEqual(wizard.create_uid, self.restricted)

    def test_describe_a_wizard(self):
        self.wizard()
        entry = next(
            entry for entry in self.result("actions")["actions"] if entry["name"] == "merge-contacts"
        )
        self.assertEqual((entry["kind"], entry["records"], entry["inputs"]), ("wizard", [2, 3], ["dst_partner_id"]))
        described = self.result("describe_action", {"action": "merge-contacts"})
        self.assertEqual(
            described["inputs"],
            [
                {
                    "name": "dst_partner_id",
                    "string": "Destination Contact",
                    "type": "many2one",
                    "required": False,
                    "relation": "res.partner",
                }
            ],
        )
        self.assertEqual((described["steps"], described["wizard"]), (["action_merge"], MERGE))
        # The same as data on the action itself, for programs that describe it.
        action = self.Action.search([("name", "=", "merge-contacts")])
        self.assertEqual(
            action.spec, {"inputs": described["inputs"], "steps": ["action_merge"], "fixed": {}}
        )
        self.assertEqual(self.open_company.spec, {"inputs": [], "steps": ["open_commercial_entity"], "fixed": {}})
        self.assertEqual(self.mark_reviewed.spec, {"inputs": [], "steps": [], "fixed": {}})
        self.assertEqual(
            described["summary"],
            "Opens the wizard Merge Partner Wizard (base.partner.merge.automatic.wizard) for "
            "2 to 3 records of Contact (res.partner). A caller may set: Destination Contact "
            "(dst_partner_id). Then presses: action_merge.",
        )

    def test_wizard_values_are_checked(self):
        self.wizard()
        first, second = self.twins()
        ids = [first.id, second.id]
        for values, reason, field in (
            # Not one of the fields a caller may set, though the wizard has it.
            ({"exclude_contact": True}, "not_writable", "exclude_contact"),
            ({"state": "finished"}, "not_writable", "state"),
            ({"no_such_field": 1}, "unknown_field", "no_such_field"),
            ({"dst_partner_id": "MCPT Twin A"}, "bad_value", "dst_partner_id"),
            # A contact the user may not read answers like one that does not exist.
            ({"dst_partner_id": self.hidden.id}, "unknown_record", "dst_partner_id"),
            ({"dst_partner_id": 999_999_999}, "unknown_record", "dst_partner_id"),
            (["dst_partner_id"], "bad_value", "values"),
        ):
            with self.subTest(values=values):
                self.assertDetails(
                    self.run_action("merge-contacts", ids, values=values), "invalid_params", reason, field
                )
        # A button and a server action take no values.
        for name in ("open-company", "mark-reviewed"):
            with self.subTest(name=name):
                self.assertDetails(
                    self.run_action(name, [first.id], values={"ref": "X"}),
                    "invalid_params",
                    "no_inputs",
                    "values",
                )
        self.assertEqual(len((first + second).exists()), 2)
        self.assertFalse(first.ref)

    def test_a_wizard_without_records_takes_them_as_values(self):
        self.wizard(
            name="merge-chosen",
            model_id=False,
            min_records=0,
            max_records=0,
            inputs="partner_ids, dst_partner_id",
        )
        first, second = self.twins()
        described = self.result("describe_action", {"action": "merge-chosen"})
        self.assertEqual(
            (described["model"], described["records"], [item["name"] for item in described["inputs"]]),
            (None, [0, 0], ["dst_partner_id", "partner_ids"]),
        )
        self.assertDetails(
            self.run_action("merge-chosen", [first.id]), "invalid_params", "record_count", "ids"
        )
        # A required input without a value is named before anything runs.
        field = self.env.registry[MERGE]._fields["dst_partner_id"]
        with patch.object(field, "required", True):
            self.assertDetails(
                self.run_action("merge-chosen", [], values={"partner_ids": [first.id, second.id]}),
                "invalid_params",
                "required_missing",
                "dst_partner_id",
            )
        result = self.ran(
            "merge-chosen",
            [],
            values={"partner_ids": [first.id, second.id], "dst_partner_id": second.id},
        )
        self.assertEqual(result["records"], [])
        self.assertFalse(first.exists())
        log = self.last_log()
        self.assertEqual(
            (log.model, log.record_ids, log.field_names, log.row_count),
            (MERGE, False, "dst_partner_id,partner_ids", 0),
        )

    def test_the_record_field_and_the_fixed_values(self):
        action = self.wizard(
            name="skip-contacts",
            steps="action_skip",
            inputs=False,
            record_field="partner_ids",
            defaults='{"exclude_contact": true, "maximum_group": 7}',
        )
        first, second = self.twins()
        data_api = self.env["o3p.mcp.api"].with_user(self.restricted)
        self.assertEqual(
            data_api._wizard_context(action, (first + second).with_user(self.restricted)),
            {
                "default_exclude_contact": True,
                "default_maximum_group": 7,
                "active_model": "res.partner",
                "active_ids": [first.id, second.id],
                "active_id": first.id,
                "default_partner_ids": [first.id, second.id],
            },
        )
        # A caller sets neither the record field nor a fixed value.
        inputs = self.result("describe_action", {"action": "skip-contacts"})["inputs"]
        names = [item["name"] for item in inputs]
        self.assertIn("dst_partner_id", names)
        self.assertNotIn("partner_ids", names)
        self.assertNotIn("exclude_contact", names)
        self.assertNotIn("state", names)
        self.assertIn(
            "Always sets: A user associated to the contact (exclude_contact) = true, "
            "Maximum of Group of Contacts (maximum_group) = 7.",
            action.summary,
        )
        self.assertDetails(
            self.run_action("skip-contacts", [first.id, second.id], values={"exclude_contact": False}),
            "invalid_params",
            "not_writable",
            "exclude_contact",
        )
        result = self.ran("skip-contacts", [first.id, second.id])
        self.assertEqual(result["form"]["exclude_contact"], True)
        self.assertEqual(result["form"]["maximum_group"], 7)
        self.assertEqual(len((first + second).exists()), 2)

    def test_a_wizard_that_hands_over_and_an_input_default(self):
        self.wizard(name="skip-then-merge", steps="action_skip, action_merge", inputs="maximum_group")
        first, second = self.twins()
        Wizard = type(self.env[MERGE])
        pressed = []

        def hand_over(wizard):
            """As a wizard that answers with a fresh wizard for its next page."""
            successor = wizard.copy()
            pressed.append(("action_skip", wizard.id, successor.id))
            return {"type": "ir.actions.act_window", "res_model": MERGE, "res_id": successor.id}

        def merge(wizard):
            pressed.append(("action_merge", wizard.id))
            return False

        with patch.object(Wizard, "action_skip", hand_over), patch.object(Wizard, "action_merge", merge):
            self.ran("skip-then-merge", [first.id, second.id])
        (_, opened, successor), (_, merged) = pressed
        self.assertNotEqual(opened, successor)
        self.assertEqual(merged, successor)
        # The default a new wizard starts with is told with the input.
        field = self.env.registry[MERGE]._fields["maximum_group"]
        with patch.object(field, "default", lambda wizard: 5):
            (item,) = self.result("describe_action", {"action": "skip-then-merge"})["inputs"]
        self.assertEqual((item["name"], item["type"], item["default"]), ("maximum_group", "integer", 5))

    def test_a_button_that_raises_undoes_the_wizard(self):
        self.wizard()
        first, second = self.twins()
        second.email = "other@example.com"
        wizards = self.env[MERGE].search_count([])
        response = self.run_action("merge-contacts", [first.id, second.id])
        self.assertApiError(response, "query_error")
        self.assertIn("same email", response.json()["error"]["details"]["message"])
        self.assertEqual(len((first + second).exists()), 2)
        self.assertEqual(self.env[MERGE].search_count([]), wizards)

    def test_a_wizard_needs_its_access_rows(self):
        self.wizard()
        first, second = self.twins()
        # Without Odoo's contact manager group there is no access to the wizard.
        self.assertNotIn("merge-contacts", self.names(user="reader_only"))
        self.assertApiError(
            self.call("describe_action", {"action": "merge-contacts"}, user="reader_only"),
            "access_denied",
        )
        self.assertApiError(
            self.run_action("merge-contacts", [first.id, second.id], user="reader_only"), "access_denied"
        )
        self.assertEqual(len((first + second).exists()), 2)

    def test_wizards_that_cannot_be_exposed(self):
        model = lambda name: self.env["ir.model"]._get(name).id  # noqa: E731
        refused = {
            "the settings": {"wizard_model_id": model("res.config.settings"), "steps": "execute"},
            "the password wizard": {
                "wizard_model_id": model("change.password.wizard"),
                "steps": "change_password_button",
            },
            "the module upgrade": {
                "wizard_model_id": model("base.module.upgrade"),
                "steps": "upgrade_module",
            },
            "a model that is not a wizard": {"wizard_model_id": model("res.partner")},
            "no button to press": {"steps": False},
            "a private button": {"steps": "action_merge, _merge"},
            "a method every model has": {"steps": "unlink"},
            "a record field that links elsewhere": {"record_field": "current_line_id"},
            "a record field that is no link": {"record_field": "state"},
            "one record field for several records": {"record_field": "dst_partner_id", "inputs": False},
            "fixed values that are not JSON": {"defaults": "{exclude_contact: true}"},
            "fixed values that are no object": {"defaults": "[1]"},
            "a fixed value for a missing field": {"defaults": '{"no_such_field": 1}'},
            "an input the wizard does not have": {"inputs": "no_such_field"},
            "a read-only input": {"inputs": "state"},
            "an input the action fixes": {
                "inputs": "exclude_contact",
                "defaults": '{"exclude_contact": true}',
            },
            "records without their model": {"model_id": False},
        }
        for case, values in refused.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                self.wizard(**values)

    def test_every_wizard_is_refused_or_reviewed(self):
        data_api = self.env["o3p.mcp.api"]
        unreviewed = [
            name
            for name in sorted(self.env.registry)
            if self.env[name]._transient
            and not self.env[name]._abstract
            and data_api._wizard_can_be_exposed(name)
            and name not in REVIEWED_WIZARDS
        ]
        self.assertEqual(
            unreviewed,
            [],
            "Refuse these wizards (WIZARD_NEVER) or review them in REVIEWED_WIZARDS with what they do.",
        )
        for name in (
            "res.config.settings",
            "change.password.wizard",
            "base.module.uninstall",
            "base.import.module",
            "server.action.history.wizard",
            "privacy.lookup.wizard",
            "calendar.provider.config",
            "snailmail.letter.format.error",
        ):
            with self.subTest(name=name):
                self.assertFalse(data_api._wizard_can_be_exposed(name))
        self.assertIn("mail.*", WIZARD_NEVER)

    def test_the_form_adds_wizard_inputs_by_name(self):
        destination = self.env["ir.model.fields"]._get(MERGE, "dst_partner_id")
        with Form(self.Action) as form:
            form.title = "Merge, from the form"
            form.name = "merge-from-form"
            form.description = "Merges contacts."
            form.kind = "wizard"
            form.wizard_model_id = self.env["ir.model"]._get(MERGE)
            form.model_id = self.partner_model
            form.min_records = 2
            form.max_records = 3
            form.steps = "action_merge"
            form.input_add_id = destination
            self.assertEqual(form.inputs, "dst_partner_id")
            self.assertFalse(form.input_add_id)
            self.assertIn("Destination Contact (dst_partner_id)", form.summary)
        self.assertEqual(form.record._input_names(), ["dst_partner_id"])
