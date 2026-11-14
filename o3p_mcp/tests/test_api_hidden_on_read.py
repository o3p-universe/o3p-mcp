"""Models that hide data in a read override.

Odoo blanks other users' private calendar events when they are read, but a
domain, an order or a group would still match them; such models are only
filtered, sorted and grouped on ``id`` and their public fields.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command
from odoo.tests import new_test_user, tagged

from .api_common import READ_SCOPES, O3pMcpApiCase
from .common import RESOURCE


@tagged("post_install", "-at_install")
class TestHiddenOnReadGeneric(O3pMcpApiCase):
    def test_only_id_is_filterable(self):
        api_class = type(self.env["o3p.mcp.api"])
        with patch.object(
            api_class, "_model_hides_on_read", lambda api, name: name == "res.partner.category"
        ):
            base = {"model": "res.partner.category", "fields": ["name"]}
            for params in (
                {"domain": [["name", "ilike", "MCP"]]},
                {"order": "name asc"},
                {"domain": [["id", "=", self.visible.id]], "order": "name desc"},
            ):
                with self.subTest(params=params):
                    self.assertApiError(self.call("search", {**base, **params}), "invalid_params")
            for params in (
                {"groupby": ["name"], "aggregates": ["__count"]},
                {"groupby": ["color"], "aggregates": ["__count"], "domain": [["name", "=", "x"]]},
                {"groupby": ["color"], "aggregates": ["name:count_distinct"]},
            ):
                with self.subTest(params=params):
                    self.assertApiError(
                        self.call("group", {"model": "res.partner.category", **params}),
                        "invalid_params",
                    )
            # Through relations too.
            for domain in (
                [["category_id.name", "ilike", "MCP"]],
                [["category_id", "ilike", "MCP"]],
                [["category_id", "any", [["name", "ilike", "MCP"]]]],
            ):
                with self.subTest(domain=domain):
                    self.assertApiError(
                        self.call("search", {"model": "res.partner", "domain": domain, "fields": ["name"]}),
                        "invalid_params",
                    )
            # Reading and filtering on id still work.
            result = self.result(
                "search", {**base, "domain": [["id", "=", self.visible.id]], "order": "id desc"}
            )
            self.assertEqual(result["records"], [{"id": self.visible.id, "name": "MCP visible"}])
            groups = self.result(
                "group",
                {"model": "res.partner",
                 "domain": [["name", "=like", "MCPT %"], ["category_id", "any", [["id", "=", self.visible.id]]]],
                 "groupby": ["type"], "aggregates": ["__count"]},
            )["groups"]
            self.assertEqual(groups, [{"type": "contact", "__count": 2}])


@tagged("post_install", "-at_install")
class TestPrivateCalendarEvents(O3pMcpApiCase):
    """Carol, an MCP user, against Bob's private event."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "calendar.event" not in cls.env:
            raise unittest.SkipTest("calendar is not installed")
        env = cls.env
        cls.bob = new_test_user(env, "o3p_mcp_bob", groups="base.group_user", name="Bob")
        carol = new_test_user(
            env, "o3p_mcp_carol", groups="base.group_user,o3p_mcp.group_mcp_user", name="Carol"
        )
        start = datetime(2026, 12, 1, 9, 0)
        cls.event = env["calendar.event"].with_user(cls.bob).create(
            {
                "name": "Secret Salary Review",
                "description": "<p>a raise of 12 percent</p>",
                "location": "Board room",
                "privacy": "private",
                "user_id": cls.bob.id,
                "partner_ids": [Command.set([cls.bob.partner_id.id])],
                "start": start,
                "stop": start + timedelta(hours=1),
            }
        )
        cls.tokens["carol"] = env["o3p.mcp.token"]._issue(
            cls.client, carol, READ_SCOPES, RESOURCE
        )[1]["access_token"]
        env.flush_all()

    def search(self, **params):
        return self.call("search", {"model": "calendar.event", "fields": ["name"], **params}, user="carol")

    def test_odoo_blanks_the_event_when_read(self):
        records = self.result(
            "read", {"model": "calendar.event", "ids": [self.event.id], "fields": ["name", "location"]},
            user="carol",
        )["records"]
        self.assertEqual(records, [{"id": self.event.id, "name": "Busy", "location": False}])

    def test_domains_orders_and_groups_on_private_fields_are_refused(self):
        bob_partner = self.bob.partner_id.id
        for domain in (
            [["name", "ilike", "Secret Salary"]],
            [["description", "ilike", "12 percent"]],
            [["location", "ilike", "Board"]],
            [["display_name", "ilike", "Secret Salary"]],
            [["partner_ids", "in", [bob_partner]]],
            [["partner_ids.name", "=", "Bob"]],
        ):
            with self.subTest(domain=domain):
                self.assertApiError(self.search(domain=domain), "invalid_params")
        self.assertApiError(
            self.search(domain=[["id", "=", self.event.id]], order="name asc"), "invalid_params"
        )
        for params in (
            {"domain": [["name", "ilike", "Secret"]], "groupby": ["user_id"], "aggregates": ["id:count"]},
            {"domain": [["description", "ilike", "percent"]], "groupby": ["privacy"], "aggregates": ["duration:sum"]},
            {"domain": [["id", "=", self.event.id]], "groupby": ["name"], "aggregates": ["__count"]},
            {"groupby": ["user_id"], "aggregates": ["name:count_distinct"]},
        ):
            with self.subTest(params=params):
                self.assertApiError(
                    self.call("group", {"model": "calendar.event", **params}, user="carol"),
                    "invalid_params",
                )
        # From the attendees' side.
        for domain in (
            [["meeting_ids.name", "ilike", "Secret"]],
            [["meeting_ids", "ilike", "Secret"]],
            [["meeting_ids", "any", [["location", "ilike", "Board"]]]],
        ):
            with self.subTest(domain=domain):
                self.assertApiError(
                    self.call("search", {"model": "res.partner", "domain": domain, "fields": ["name"]}, user="carol"),
                    "invalid_params",
                )

    def test_public_fields_still_work(self):
        response = self.search(domain=[["user_id", "=", self.bob.id], ["privacy", "=", "private"]], order="start desc")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["records"], [{"id": self.event.id, "name": "Busy"}])
        groups = self.result(
            "group",
            {"model": "calendar.event", "domain": [["user_id", "=", self.bob.id]],
             "groupby": ["user_id"], "aggregates": ["duration:sum", "id:count"]},
            user="carol",
        )["groups"]
        # Busy time is public: the private event counts with its duration.
        self.assertEqual(groups, [{"user_id": [self.bob.id, "Bob"], "duration:sum": 1.0, "id:count": 1}])
        self.assertTrue(self.env["o3p.mcp.api"]._model_hides_on_read("calendar.event"))
