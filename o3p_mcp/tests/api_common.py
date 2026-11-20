import base64
import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command
from odoo.tests import HttpCase, new_test_user

from ..models.api import ERRORS
from .common import RESOURCE, O3pMcpCommon


API_URL = "/o3p_mcp/api/v1/"
READ_SCOPES = {"okf.read", "odoo.read"}


def basic(client_id, secret):
    raw = f"{client_id}:{secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


class O3pMcpApiCase(O3pMcpCommon, HttpCase):
    """Three users and a set of partners, some hidden by an access restriction.

    - admin: the administrator, in the MCP User group;
    - restricted: an internal MCP user who may only read partners in the
      "MCP visible" category (a global read restriction scoped to that user);
    - portal: a portal user placed in the MCP User group.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["ir.config_parameter"].sudo().set_str("o3p_mcp.resource_url", RESOURCE)
        cls.server = env["o3p.mcp.client"].create(
            {
                "name": "MCP server",
                "kind": "resource_server",
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        cls.server_secret = cls.server._set_new_secret()
        cls.admin = env.ref("base.user_admin")
        cls.admin.write(
            {"group_ids": [Command.link(cls.user_group.id), Command.link(cls.sql_group.id)]}
        )
        cls.restricted = new_test_user(
            env,
            "o3p_mcp_restricted",
            groups="base.group_user,o3p_mcp.group_mcp_user,o3p_mcp.group_mcp_sql",
            name="Restricted User",
        )
        cls.portal = new_test_user(
            env,
            "o3p_mcp_portal",
            groups="base.group_portal,o3p_mcp.group_mcp_user",
            name="Portal User",
        )
        cls.visible = env["res.partner.category"].create({"name": "MCP visible"})
        Partner = env["res.partner"]
        cls.alpha = Partner.create(
            {"name": "MCPT Alpha", "color": 3, "category_id": [Command.link(cls.visible.id)]}
        )
        cls.beta = Partner.create(
            {"name": "MCPT Beta", "color": 4, "category_id": [Command.link(cls.visible.id)]}
        )
        cls.hidden = Partner.create({"name": "MCPT Hidden One", "color": 100})
        cls.hidden2 = Partner.create({"name": "MCPT Hidden Two", "color": 200})
        cls.restricted.partner_id.category_id = [Command.link(cls.visible.id)]
        env["ir.access"].create(
            {
                "name": "o3p_mcp test: the restricted user reads visible partners only",
                "model_id": env.ref("base.model_res_partner").id,
                "operation": "r",
                "domain": (
                    f"[('category_id', 'in', [{cls.visible.id}])] "
                    f"if user.id == {cls.restricted.id} else []"
                ),
            }
        )
        Token = env["o3p.mcp.token"]
        cls.tokens = {
            name: Token._issue(cls.client, user, READ_SCOPES, RESOURCE)[1]["access_token"]
            for name, user in (
                ("admin", cls.admin),
                ("restricted", cls.restricted),
                ("portal", cls.portal),
                ("editor", cls.editor),
            )
        }

    def call(self, op, params=None, user="restricted", token=None, headers=None, raw=None):
        if headers is None:
            headers = basic(self.server.client_id, self.server_secret)
        if raw is None:
            raw = json.dumps(
                {"token": token if token is not None else self.tokens[user], "params": params or {}}
            )
        return self.url_open(
            API_URL + op,
            data=raw,
            headers={"Content-Type": "application/json", **headers},
        )

    def result(self, op, params=None, user="restricted"):
        response = self.call(op, params, user=user)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(list(body), ["result"])
        return body["result"]

    def assertApiError(self, response, code, status=None):
        expected_status = ERRORS[code][0] if status is None else status
        self.assertEqual(response.status_code, expected_status, response.text)
        body = response.json()
        self.assertEqual(list(body), ["error"])
        self.assertLessEqual(set(body["error"]), {"code", "message", "details"})
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return body["error"]["message"]

    def assertDetails(self, response, code, reason, field=None, model=None):
        """Check the error's details: the fixed reason and hint, and where."""
        from ..models.api import MODEL_HINTS, REASONS

        self.assertApiError(response, code)
        details = response.json()["error"]["details"]
        expected = {}
        if field is not None:
            expected["field"] = field
        if model is not None:
            expected["model"] = model
        expected["reason"] = reason
        expected["hint"] = (reason == "no_model_access" and MODEL_HINTS.get(model)) or REASONS[reason]
        self.assertEqual(details, expected)
        return details

    @contextmanager
    def protected_field(self, model, field_name, groups="base.group_system"):
        """Give ``field_name`` a ``groups`` restriction for the duration."""
        field = self.env.registry[model]._fields[field_name]
        with patch.object(field, "groups", groups):
            yield
