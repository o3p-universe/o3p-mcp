import json
import os
import unittest

from odoo import Command
from odoo.tests import new_test_user

from ..models.api import ApiError
from .api_common import O3pMcpApiCase
from ..models.oauth import set_config
from .common import RESOURCE


SQL_SCOPES = {"okf.read", "odoo.read", "odoo.sql"}
# Strings the restricted users must never receive, in rows, errors or
# anywhere else in an answer.
CONFIG_MARKER = "MCPT-CFG-MARKER-7d4f"
FIELD_MARKER = "MCPT-FIELD-MARKER-41c2"
HIDDEN_MARKER = "MCPT Hidden"
SECRET_CATEGORY = "MCPT Secret Category"
PASSWORD_MARKER = "$pbkdf2"


class O3pMcpSqlCase(O3pMcpApiCase):
    """The data API fixture plus a configured sandbox and more users.

    - restricted: partners in the "MCP visible" category only (api_common);
    - restricted2: partners of color < 150 only, and no "MCPT Secret Category";
    - multi: an internal user of the main company and company B;
    - admin: the administrator, main company only.

    The sandbox comes from O3P_SQL_TEST_HOST and O3P_SQL_PASSWORD, which
    scripts/run-tests.sh provides; without them the SQL tests are skipped.
    """

    @classmethod
    def setUpClass(cls):
        cls.sandbox_host = os.environ.get("O3P_SQL_TEST_HOST")
        if not cls.sandbox_host or not os.environ.get("O3P_SQL_PASSWORD"):
            raise unittest.SkipTest("No SQL sandbox: set O3P_SQL_TEST_HOST and O3P_SQL_PASSWORD.")
        super().setUpClass()
        env = cls.env
        set_config(env, "o3p_mcp.sql_host", cls.sandbox_host)
        set_config(env, "ir.o3p_mcp_test_marker", CONFIG_MARKER)
        cls.company_b = env["res.company"].create({"name": "MCPT Company B"})
        main = env.ref("base.main_company")
        cls.restricted2 = new_test_user(
            env,
            "o3p_mcp_restricted2",
            groups="base.group_user,o3p_mcp.group_mcp_user",
            name="Second Restricted User",
            password="o3p_mcp_restricted2-password",
        )
        cls.multi = new_test_user(
            env,
            "o3p_mcp_multi",
            groups="base.group_user,o3p_mcp.group_mcp_user",
            name="Multi Company User",
            company_id=main.id,
            company_ids=[Command.set([main.id, cls.company_b.id])],
        )
        Partner = env["res.partner"]
        cls.secret_category = env["res.partner.category"].create({"name": SECRET_CATEGORY})
        cls.alpha.category_id = [Command.link(cls.secret_category.id)]
        cls.alpha.comment = FIELD_MARKER
        cls.b_partner = Partner.create({"name": "MCPT Company B Partner", "company_id": cls.company_b.id, "color": 5})
        cls.b_archived = Partner.create(
            {"name": "MCPT Company B Archived", "company_id": cls.company_b.id, "active": False}
        )
        cls.archived = Partner.create({"name": "MCPT Archived", "active": False, "color": 6})
        # Odoo 18: global record rules (ir.rule) that only narrow restricted2.
        Rule = env["ir.rule"]
        for model, domain, name in (
            ("base.model_res_partner", "[('color', '<', 150)]", "reads partners of color < 150"),
            (
                "base.model_res_partner_category",
                f"[('name', '!=', {SECRET_CATEGORY!r})]",
                "never reads the secret category",
            ),
        ):
            Rule.create(
                {
                    "name": f"o3p_mcp test: restricted2 {name}",
                    "model_id": env.ref(model).id,
                    "perm_read": True,
                    "perm_write": False,
                    "perm_create": False,
                    "perm_unlink": False,
                    "domain_force": f"{domain} if user.id == {cls.restricted2.id} else []",
                }
            )
        Token = env["o3p.mcp.token"]
        cls.users = {
            "admin": cls.admin,
            "restricted": cls.restricted,
            "restricted2": cls.restricted2,
            "multi": cls.multi,
        }
        cls.sql_tokens = {
            name: Token._issue(cls.client, user, SQL_SCOPES, RESOURCE)[1]["access_token"]
            for name, user in cls.users.items()
        }
        cls.env.flush_all()

    # ------------------------------------------------------------------

    def sql_call(self, query, user="restricted", limit=None):
        params = {"query": query}
        if limit is not None:
            params["limit"] = limit
        raw = json.dumps({"token": self.sql_tokens[user], "params": params})
        return self.call("sql", raw=raw)

    def sql_result(self, query, user="restricted", limit=None):
        response = self.sql_call(query, user=user, limit=limit)
        self.assertEqual(response.status_code, 200, response.text[:1000])
        return response.json()["result"]

    def sql_error(self, query, code="query_error", user="restricted", sqlstate=None):
        response = self.sql_call(query, user=user)
        self.assertApiErrorWithDetails(response, code)
        error = response.json()["error"]
        if sqlstate is not None:
            self.assertEqual(error["details"]["sqlstate"], sqlstate, response.text)
        return error

    def assertApiErrorWithDetails(self, response, code):
        self.assertEqual(response.status_code, ApiError(code).status, response.text[:1000])
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertLessEqual(set(error), {"code", "message", "details"})
        if "details" in error:
            self.assertEqual(set(error["details"]), {"sqlstate", "position", "message"})
            self.assertLessEqual(len(error["details"]["message"]), 500)

    def direct(self, user, query, limit=1000):
        """Run the sql operation as ``user`` without HTTP; return the result or the ApiError."""
        api = self.env["o3p.mcp.api"].with_user(user).with_context(lang="en_US")
        try:
            return api._run("sql", {"query": query, "limit": limit})[0]
        except ApiError as error:
            return error
