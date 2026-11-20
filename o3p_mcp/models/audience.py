import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError


CODE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class O3pMcpAudience(models.Model):
    """A named set of Odoo groups whose users may read the same knowledge.

    The MCP server learns a user's audiences from token introspection and
    serves each knowledge bundle to the audiences its configuration names.
    """

    _name = "o3p.mcp.audience"
    _description = "MCP Audience"
    _order = "code"

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        index=True,
        copy=False,
        help="The name the MCP server's configuration uses for this audience: "
        "a lowercase letter, then lowercase letters, digits, - or _, at most "
        "32 characters.",
    )
    group_ids = fields.Many2many(
        "res.groups",
        "o3p_mcp_audience_group_rel",
        "audience_id",
        "group_id",
        string="Groups",
        help="A user holding any of these groups, directly or through another "
        "group, belongs to the audience.",
    )
    active = fields.Boolean(default=True)
    user_count = fields.Integer(string="Users", compute="_compute_user_count")

    _sql_constraints = [
        ("code_unique", "UNIQUE (code)", "An audience with this code already exists."),
    ]

    @api.constrains("code")
    def _check_code(self):
        for audience in self:
            if not CODE_RE.fullmatch(audience.code or ""):
                raise ValidationError(
                    self.env._(
                        "The code %(code)s is not valid: use a lowercase letter, then "
                        "lowercase letters, digits, - or _, at most 32 characters.",
                        code=audience.code,
                    )
                )

    @api.depends("group_ids")
    def _compute_user_count(self):
        Users = self.env["res.users"].sudo()
        for audience in self:
            audience.user_count = (
                Users.search_count(
                    [("share", "=", False), ("groups_id", "in", audience.group_ids.ids)]
                )
                if audience.group_ids
                else 0
            )
