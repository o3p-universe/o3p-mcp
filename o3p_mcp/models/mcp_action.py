import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.service.model import get_public_method


NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_ACTION_RECORDS = 100


class O3pMcpAction(models.Model):
    """Something an administrator lets MCP users do beyond saving fields.

    An exposed action is a record button (a public method called on the
    chosen records) or a server action. The data API lists the actions a
    user may run and runs one by its name; a client never names a method.
    """

    _name = "o3p.mcp.action"
    _description = "MCP Exposed Action"
    _order = "name"
    _rec_name = "title"

    name = fields.Char(
        required=True,
        index=True,
        copy=False,
        help="The name an MCP client uses for this action: a lowercase letter, "
        "then lowercase letters, digits, - or _, at most 64 characters.",
    )
    title = fields.Char(required=True, translate=True)
    description = fields.Text(
        required=True,
        translate=True,
        help="What the action does and when to use it. The assistant reads it "
        "to choose the action, so name the effect and its conditions.",
    )
    kind = fields.Selection(
        [("method", "Record button"), ("server", "Server action")],
        required=True,
        default="method",
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Records of",
        compute="_compute_model_id",
        store=True,
        readonly=False,
        precompute=True,
        ondelete="cascade",
        domain=[("transient", "=", False)],
        help="The model of the records the action runs on.",
    )
    model_name = fields.Char(related="model_id.model", string="Model name")
    min_records = fields.Integer(string="Fewest records", default=1)
    max_records = fields.Integer(string="Most records", default=1)
    method = fields.Char(
        help="The method the button calls on the records, as in the button's "
        "name in the form view. It takes no arguments."
    )
    server_action_id = fields.Many2one(
        "ir.actions.server",
        string="Server action",
        ondelete="cascade",
        domain=[("usage", "=", "ir_actions_server")],
    )
    group_ids = fields.Many2many(
        "res.groups",
        "o3p_mcp_action_group_rel",
        "action_id",
        "group_id",
        string="Only for",
        help="When set, only users holding one of these groups, directly or "
        "through another group, may run the action. Odoo's own rights apply "
        "in any case.",
    )
    active = fields.Boolean(default=True)
    summary = fields.Text(string="What it runs", compute="_compute_summary")
    problem = fields.Char(string="Cannot run", compute="_compute_summary")

    _name_unique = models.Constraint("UNIQUE (name)", "An exposed action with this name already exists.")

    @api.depends("kind", "server_action_id")
    def _compute_model_id(self):
        for action in self:
            if action.kind == "server" and action.server_action_id:
                action.model_id = action.server_action_id.model_id
            else:
                action.model_id = action.model_id

    # ------------------------------------------------------------------
    # What can be exposed
    # ------------------------------------------------------------------

    @api.model
    def _method_problem(self, model, name):
        """Say why ``name`` cannot be run as a button of ``model``, or False.

        A method qualifies when Odoo's web client could call it and it is not
        one every model has (write, unlink, copy, export_data and the like).
        """
        if not isinstance(name, str) or not name.isidentifier():
            return self.env._("%(name)s is not a method name.", name=name)
        if hasattr(type(self.env["base"]), name):
            return self.env._(
                "%(name)s is a method every model has, not a button of this model.", name=name
            )
        try:
            get_public_method(model, name)
        except AccessError:
            return self.env._("%(name)s is private: Odoo's buttons cannot call it.", name=name)
        except AttributeError:
            return self.env._(
                "%(model)s has no method %(name)s.", model=model._name, name=name
            )
        return False

    @api.model
    def _records_model_problem(self, name):
        """Say why records of model ``name`` cannot be acted on, or False."""
        if not name or name not in self.env.registry:
            return self.env._("The model does not exist.")
        if not self.env["o3p.mcp.api"]._model_can_be_opened(name):
            return self.env._(
                "%(model)s is hidden from MCP, hides data when it is read, or is one of "
                "the models that grant access, hold code or send messages.",
                model=name,
            )
        return False

    def _server_action_problem(self, server_action, depth=0):
        """Say why ``server_action`` cannot be exposed, or False."""
        if server_action.usage != "ir_actions_server":
            return self.env._("Only server actions can be exposed, not the code of a scheduled job.")
        targets = {server_action.model_id.model}
        if server_action.crud_model_id:
            targets.add(server_action.crud_model_id.model)
        for target in sorted(targets):
            problem = self._records_model_problem(target)
            if problem:
                return problem
        children = server_action.child_ids
        if children and depth >= 3:
            return self.env._("The server action nests other actions too deeply.")
        for child in children:
            problem = self._server_action_problem(child, depth + 1)
            if problem:
                return problem
        return False

    def _definition_problem(self):
        """Say why this action cannot run as it is defined, or False."""
        self.ensure_one()
        action = self.sudo()
        if not NAME_RE.fullmatch(action.name or ""):
            return self.env._(
                "The name %(name)s is not valid: use a lowercase letter, then lowercase "
                "letters, digits, - or _, at most 64 characters.",
                name=action.name,
            )
        if not 0 <= action.min_records <= action.max_records <= MAX_ACTION_RECORDS:
            return self.env._(
                "The number of records must be between 0 and %(most)s, the fewest not above the most.",
                most=MAX_ACTION_RECORDS,
            )
        if action.kind == "method":
            if not action.model_id or not action.method:
                return self.env._("A record button needs a model and a method.")
            if action.min_records < 1:
                return self.env._("A record button runs on at least one record.")
            return self._records_model_problem(action.model_id.model) or self._method_problem(
                self.env[action.model_id.model], action.method
            )
        if action.kind == "server":
            if not action.server_action_id:
                return self.env._("Choose the server action to run.")
            if action.model_id != action.server_action_id.model_id:
                return self.env._("The records must be of the server action's model.")
            return self._server_action_problem(action.server_action_id)
        return False

    @api.constrains(
        "name", "kind", "model_id", "min_records", "max_records", "method", "server_action_id"
    )
    def _check_definition(self):
        for action in self:
            problem = action._definition_problem()
            if problem:
                raise ValidationError(problem)

    # ------------------------------------------------------------------
    # In words
    # ------------------------------------------------------------------

    def _records_phrase(self):
        self.ensure_one()
        label = "%s (%s)" % (self.model_id.name, self.model_id.model)
        if self.max_records == 0:
            return self.env._("no record")
        if self.min_records == self.max_records == 1:
            return self.env._("one record of %(model)s", model=label)
        return self.env._(
            "%(fewest)s to %(most)s records of %(model)s",
            fewest=self.min_records,
            most=self.max_records,
            model=label,
        )

    def _summary(self):
        self.ensure_one()
        if self.kind == "method":
            return self.env._(
                "Presses the button %(method)s on %(records)s.",
                method=self.method,
                records=self._records_phrase(),
            )
        return self.env._(
            "Runs the server action %(action)s on %(records)s.",
            action=self.server_action_id.name,
            records=self._records_phrase(),
        )

    @api.depends(
        "name", "kind", "model_id", "min_records", "max_records", "method", "server_action_id"
    )
    def _compute_summary(self):
        for action in self:
            complete = action.kind and (action.model_id or action.server_action_id)
            action.problem = action._definition_problem() if complete else False
            action.summary = action._summary() if complete and not action.problem else False
