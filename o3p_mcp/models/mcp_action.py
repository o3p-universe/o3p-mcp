import json
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.service.model import get_public_method


NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_ACTION_RECORDS = 100
MAX_STEPS = 10
# Wizards that are never exposed, whatever their buttons do: they change the
# system or its settings, give access to people, or send messages. The models
# the data API never writes and the hidden models are refused as well.
WIZARD_NEVER = (
    # Settings, modules, languages, views, menus, server actions.
    "res.config*",
    "base.module.*",
    "base.language.*",
    "base.import.*",
    "base_import.*",
    "base.document.layout",
    "base.enable.profiling.wizard",
    "reset.view.arch.wizard",
    "wizard.ir.model.menu.create",
    "server.action.*",
    # Passwords, sign-in devices, credentials, access for people outside.
    "change.password.*",
    "auth.*",
    "auth_*",
    "portal.*",
    "*.share.wizard",
    "*.share.collaborator.wizard",
    "calendar.provider.*",
    # Messages, letters and the lists that stop them.
    "mail.*",
    "digest.*",
    "snailmail.*",
    "phone.blacklist.*",
    # Looking a person up across every model.
    "privacy.*",
)


def names_of(value):
    """Return the names in ``value``, a list separated by commas or lines."""
    return [name.strip() for name in (value or "").replace("\n", ",").split(",") if name.strip()]


class O3pMcpAction(models.Model):
    """Something an administrator lets MCP users do beyond saving fields.

    An exposed action is a record button (a public method called on the
    chosen records), a server action, or a wizard: a transient model that is
    created for the chosen records with the caller's values and whose buttons
    are then pressed in order. The data API lists the actions a user may run
    and runs one by its name; a client never names a method.
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
        [("method", "Record button"), ("server", "Server action"), ("wizard", "Wizard")],
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
        help="The model of the records the action runs on. A wizard that "
        "takes no record has none.",
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
    wizard_model_id = fields.Many2one(
        "ir.model",
        string="Wizard",
        ondelete="cascade",
        domain=[("transient", "=", True)],
        help="The wizard that asks for the values: its fields are what a "
        "caller may set, its buttons do the work.",
    )
    wizard_model_name = fields.Char(related="wizard_model_id.model", string="Wizard name")
    record_field = fields.Char(
        string="Field for the records",
        help="The wizard's field that receives the record (a many-to-one) or "
        "the records (a many-to-many). Leave it empty when the wizard takes "
        "them from active_id or active_ids itself.",
    )
    defaults = fields.Text(
        string="Fixed values",
        help='Values the wizard always starts with, as a JSON object of its '
        'fields, for example {"operation": "cancel"}: what the context of '
        "the button that opens the wizard sets. A caller cannot change them.",
    )
    steps = fields.Char(
        string="Buttons to press",
        help="The methods of the wizard's buttons, in the order to press "
        "them, separated by commas: for example action_review, action_apply.",
    )
    inputs = fields.Char(
        string="Fields a caller may set",
        help="Names of wizard fields, separated by commas. Empty: every field "
        "of the wizard that can be written through MCP.",
    )
    input_add_id = fields.Many2one(
        "ir.model.fields",
        string="Add a field",
        store=False,
        domain="[('model_id', '=', wizard_model_id), ('readonly', '=', False), "
        "('related', '=', False), ('ttype', 'in', ('char', 'text', 'html', 'integer', 'float', "
        "'monetary', 'boolean', 'date', 'datetime', 'selection', 'many2one', 'many2many'))]",
        help="Pick a field of the wizard to add it to the fields a caller may set.",
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
    spec = fields.Json(
        string="Inputs and steps",
        compute="_compute_spec",
        help="What the action asks for and presses, as data: the inputs with "
        "their label, type and default as the reader gets them, the buttons "
        "pressed in order and the fixed values. Read by programs that describe "
        "the exposed actions, such as a knowledge producer.",
    )

    _sql_constraints = [
        ("name_unique", "UNIQUE (name)", "An exposed action with this name already exists."),
    ]

    @api.depends("kind", "server_action_id")
    def _compute_model_id(self):
        for action in self:
            if action.kind == "server" and action.server_action_id:
                action.model_id = action.server_action_id.model_id
            else:
                action.model_id = action.model_id

    @api.onchange("input_add_id")
    def _onchange_input_add_id(self):
        """Add the picked field to the fields a caller may set."""
        name = self.input_add_id.name
        self.input_add_id = False
        if name and name not in names_of(self.inputs):
            self.inputs = ", ".join([*names_of(self.inputs), name])

    @api.onchange("wizard_model_id", "model_id")
    def _onchange_wizard_model_id(self):
        """Propose the wizard field that points to the records' model, when
        there is exactly one."""
        if self.kind != "wizard" or self.record_field:
            return
        wizard = self.env.get(self.wizard_model_id.model)
        if wizard is None or not self.model_id:
            return
        candidates = [
            name
            for name, field in wizard._fields.items()
            if field.type in ("many2one", "many2many")
            and field.comodel_name == self.model_id.model
            and name not in models.MAGIC_COLUMNS
        ]
        if len(candidates) == 1:
            self.record_field = candidates[0]

    # ------------------------------------------------------------------
    # The parts of a wizard action
    # ------------------------------------------------------------------

    def _step_names(self):
        self.ensure_one()
        return names_of(self.steps)

    def _input_names(self):
        self.ensure_one()
        return names_of(self.inputs)

    def _default_values(self):
        """Return the fixed values of the wizard, or None when they are not
        a JSON object."""
        self.ensure_one()
        if not (self.defaults or "").strip():
            return {}
        try:
            values = json.loads(self.defaults)
        except ValueError:
            return None
        return values if isinstance(values, dict) else None

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

    def _wizard_problem(self):
        """Say why this wizard action cannot run as it is defined, or False."""
        action = self
        data_api = self.env["o3p.mcp.api"]
        name = action.wizard_model_id.model
        if not name or name not in self.env.registry:
            return self.env._("Choose the wizard.")
        if not data_api._wizard_can_be_exposed(name):
            return self.env._(
                "%(model)s cannot be exposed: it is not a wizard, it is hidden from MCP, or it "
                "is one of the wizards that change the system, give access or send messages.",
                model=name,
            )
        wizard = self.env[name]
        steps = action._step_names()
        if not 0 < len(steps) <= MAX_STEPS:
            return self.env._(
                "Name the wizard's buttons to press, at most %(most)s.", most=MAX_STEPS
            )
        for step in steps:
            problem = self._method_problem(wizard, step)
            if problem:
                return problem
        if action.model_id:
            problem = self._records_model_problem(action.model_id.model)
            if problem:
                return problem
        elif action.max_records or action.record_field:
            return self.env._("A wizard that takes records needs their model.")
        if action.record_field:
            field = wizard._fields.get(action.record_field)
            if (
                field is None
                or field.type not in ("many2one", "many2many")
                or field.comodel_name != action.model_id.model
            ):
                return self.env._(
                    "%(field)s is not a field of the wizard that links to %(model)s.",
                    field=action.record_field,
                    model=action.model_id.model,
                )
            if field.type == "many2one" and action.max_records > 1:
                return self.env._(
                    "%(field)s takes one record: the action cannot take more.",
                    field=action.record_field,
                )
        fixed = action._default_values()
        if fixed is None:
            return self.env._("The fixed values are not a JSON object.")
        for key in fixed:
            if key not in wizard._fields or key in models.MAGIC_COLUMNS:
                return self.env._("The wizard has no field %(field)s to fix a value for.", field=key)
        taken = {action.record_field, *fixed}
        for key in action._input_names():
            field = wizard._fields.get(key)
            if field is None or not data_api._field_takes_values(field):
                return self.env._(
                    "%(field)s is not a field of the wizard that can be written through MCP.",
                    field=key,
                )
            if key in taken:
                return self.env._(
                    "%(field)s is set by the action itself, so a caller cannot set it.", field=key
                )
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
        return action._wizard_problem()

    @api.constrains(
        "name", "kind", "model_id", "min_records", "max_records", "method", "server_action_id",
        "wizard_model_id", "record_field", "defaults", "steps", "inputs",
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
        if self.max_records == 0 or not self.model_id:
            return self.env._("no record")
        label = "%s (%s)" % (self.model_id.name, self.model_id.model)
        if self.min_records == self.max_records == 1:
            return self.env._("one record of %(model)s", model=label)
        return self.env._(
            "%(fewest)s to %(most)s records of %(model)s",
            fewest=self.min_records,
            most=self.max_records,
            model=label,
        )

    def _wizard_input_fields(self):
        """Return the wizard fields a caller may set, whoever the caller is."""
        self.ensure_one()
        wizard = self.env[self.wizard_model_id.model]
        data_api = self.env["o3p.mcp.api"]
        listed = self._input_names()
        taken = {self.record_field, *(self._default_values() or {})}
        return {
            name: field
            for name, field in wizard._fields.items()
            if name not in taken
            and (name in listed if listed else True)
            and data_api._field_takes_values(field)
            and not data_api._field_denied(field)
        }

    def _summary(self):
        self.ensure_one()
        if self.kind == "method":
            return self.env._(
                "Presses the button %(method)s on %(records)s.",
                method=self.method,
                records=self._records_phrase(),
            )
        if self.kind == "server":
            return self.env._(
                "Runs the server action %(action)s on %(records)s.",
                action=self.server_action_id.name,
                records=self._records_phrase(),
            )
        wizard = self.env[self.wizard_model_id.model]
        labels = wizard.fields_get(attributes=["string"])
        label = lambda name: "%s (%s)" % (labels.get(name, {}).get("string") or name, name)  # noqa: E731
        parts = [
            self.env._(
                "Opens the wizard %(wizard)s (%(model)s) for %(records)s.",
                wizard=self.wizard_model_id.name,
                model=self.wizard_model_id.model,
                records=self._records_phrase(),
            )
        ]
        fixed = self._default_values()
        if fixed:
            parts.append(
                self.env._(
                    "Always sets: %(values)s.",
                    values=", ".join(
                        "%s = %s" % (label(key), json.dumps(value, ensure_ascii=False))
                        for key, value in fixed.items()
                    ),
                )
            )
        asked = sorted(self._wizard_input_fields())
        parts.append(
            self.env._("A caller may set: %(fields)s.", fields=", ".join(map(label, asked)))
            if asked
            else self.env._("A caller sets nothing.")
        )
        parts.append(
            self.env._("Then presses: %(steps)s.", steps=", ".join(self._step_names()))
        )
        return " ".join(parts)

    @api.depends(
        "name", "kind", "model_id", "min_records", "max_records", "method", "server_action_id",
        "wizard_model_id", "record_field", "defaults", "steps", "inputs",
    )
    def _compute_summary(self):
        for action in self:
            complete = action.kind and (
                action.model_id or action.server_action_id or action.wizard_model_id
            )
            action.problem = action._definition_problem() if complete else False
            action.summary = action._summary() if complete and not action.problem else False

    def _compute_spec(self):
        data_api = self.env["o3p.mcp.api"]
        steps = {"method": lambda action: [action.method], "wizard": lambda action: action._step_names()}
        for action in self:
            if not action.kind or action._definition_problem():
                action.spec = False
                continue
            action.spec = {
                "inputs": data_api._input_descriptions(action),
                "steps": steps.get(action.kind, lambda action: [])(action),
                "fixed": action._default_values() if action.kind == "wizard" else {},
            }
