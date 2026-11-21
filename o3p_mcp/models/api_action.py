import fnmatch
from contextlib import contextmanager

from odoo import models

from .api import (
    MAX_MODELS,
    MAX_MODELS_OFFSET,
    MAX_QUERY_LENGTH,
    MSG_RANGE,
    SAFE_NAME_RE,
    invalid,
)
from .api_write import MAGIC_FIELDS, MAX_MESSAGE, MSG_VALUES, MSG_WRITE, WRITE_NEVER, refused
from .mcp_action import NAME_RE, WIZARD_NEVER


MAX_NAME = 200
# The longest text of a wizard field in an answer: a review summary fits.
MAX_FORM_TEXT = 4000
FORM_TYPES = frozenset(
    {"char", "text", "integer", "float", "monetary", "boolean", "date", "datetime", "selection",
     "many2one", "many2many"}
)
MSG_COMMITS = (
    "The action ends the database transaction itself, which is not allowed "
    "through MCP: nothing was changed."
)
_MISSING = object()


class O3pMcpApiAction(models.AbstractModel):
    _inherit = "o3p.mcp.api"

    # ------------------------------------------------------------------
    # The actions a user may run
    # ------------------------------------------------------------------

    def _wizard_can_be_exposed(self, name):
        """Tell whether the wizard ``name`` may be exposed at all: a transient
        model that is not hidden and not one that changes the system, gives
        access or sends messages."""
        if not isinstance(name, str) or name not in self.env.registry:
            return False
        model = self.env[name]
        return bool(
            model._transient
            and not model._abstract
            and not self._model_denied(name)
            and not any(fnmatch.fnmatchcase(name, item) for item in (*WRITE_NEVER, *WIZARD_NEVER))
        )

    def _action_for_user(self, action):
        """Tell whether the user is one of those ``action`` is exposed to."""
        return not action.group_ids or bool(action.group_ids & self.env.user.groups_id)

    def _action_rights_problem(self, action):
        """Name the Odoo right the user lacks to run ``action``, or None.

        An action changes its records, so the user needs Odoo's write access
        on their model, as Odoo requires for a server action; a server action
        limited to groups needs one of them as well.
        """
        if action.model_id:
            model = self.env[action.model_id.model]
            if not model.has_access("read"):
                return "no_model_access"
            if not model.has_access("write"):
                return "no_write_access"
        if action.kind == "server":
            groups = action.server_action_id.groups_id
            if groups and not groups & self.env.user.groups_id:
                return "no_write_access"
        if action.kind == "wizard" and not self.env[action.wizard_model_id.model].has_access("create"):
            return "no_wizard_access"
        return None

    def _action_inputs(self, action):
        """Return the wizard fields the user may set through ``action``."""
        if action.kind != "wizard":
            return {}
        wizard = self.env[action.wizard_model_id.model]
        allowed = action._wizard_input_fields()
        return {
            name: field
            for name, field in wizard._fields.items()
            if name in allowed
            and field.is_accessible(wizard.env)
        }

    def _wizard_context(self, action, records):
        """Return the context a wizard of ``action`` opens with for
        ``records``: the active records, as Odoo's web client names them, the
        record in the wizard's own field and the action's fixed values."""
        context = {f"default_{key}": value for key, value in action._default_values().items()}
        if records is None:
            return context
        context.update(
            active_model=records._name,
            active_ids=records.ids,
            active_id=records.ids[0] if records else False,
        )
        if action.record_field and records:
            wizard = self.env[action.wizard_model_id.model]
            single = wizard._fields[action.record_field].type == "many2one"
            context[f"default_{action.record_field}"] = records.id if single else records.ids
        return context

    def _input_descriptions(self, action):
        """Describe the inputs of ``action`` for the user: label, type,
        relation, selection values, whether required, help, and the default
        a new wizard gets without a record."""
        inputs = self._action_inputs(action)
        if not inputs:
            return []
        wizard = self.env[action.wizard_model_id.model].with_context(
            self._wizard_context(action, None)
        )
        descriptions = wizard.fields_get(
            list(inputs), attributes=["string", "type", "relation", "required", "selection", "help"]
        )
        try:
            with self.env.cr.savepoint():
                defaults = wizard.default_get(list(inputs))
        except Exception:  # noqa: BLE001 - a wizard may need its record to compute defaults
            defaults = {}
        result = []
        for name in sorted(descriptions):
            description = descriptions[name]
            item = {
                "name": name,
                "string": description.get("string") or name,
                "type": description.get("type"),
                "required": bool(description.get("required")),
            }
            if description.get("relation"):
                item["relation"] = description["relation"]
            if isinstance(description.get("selection"), (list, tuple)):
                item["selection"] = [[key, label] for key, label in description["selection"]]
            if description.get("help"):
                item["help"] = description["help"]
            default = defaults.get(name)
            if type(default) in (str, int, float, bool) and default not in (False, ""):
                item["default"] = default[:MAX_NAME] if isinstance(default, str) else default
            result.append(item)
        return result

    def _runnable_actions(self):
        """Return the exposed actions the user may run now, by name."""
        actions = self.env["o3p.mcp.action"].sudo().search([])
        return actions.filtered(
            lambda action: self._action_for_user(action)
            and not action._definition_problem()
            and not self._action_rights_problem(action)
        )

    def _action(self, name):
        """Return the exposed action ``name`` for the user, or raise.

        A name that does not exist and an action exposed to other groups get
        the same answer.
        """
        action = self.env["o3p.mcp.action"].sudo()
        if isinstance(name, str) and NAME_RE.fullmatch(name):
            action = action.search([("name", "=", name)], limit=1)
        if not action or not self._action_for_user(action):
            raise invalid("unknown_action", "action")
        if action._definition_problem():
            raise invalid("action_unavailable", "action")
        return action

    def _action_entry(self, action):
        return {
            "name": action.name,
            "title": action.title,
            "description": action.description,
            "kind": action.kind,
            "model": action.model_id.model or None,
            "records": [action.min_records, action.max_records],
            "inputs": sorted(self._action_inputs(action)),
        }

    # ------------------------------------------------------------------
    # Running an action
    # ------------------------------------------------------------------

    def _action_records(self, action, ids):
        """Return the records ``ids`` that ``action`` runs on, or raise.

        There are as many as the action takes, and the user may read and
        change each of them: an id that does not exist and one the user may
        not read get the same answer.
        """
        if ids is None:
            ids = []
        if not isinstance(ids, list) or not all(type(item) is int and item > 0 for item in ids):
            raise invalid("bad_value", "ids")
        ids = list(dict.fromkeys(ids))
        if not action.min_records <= len(ids) <= action.max_records:
            raise invalid("record_count", "ids")
        self._check_action_rights(action)
        if not action.model_id:
            return None
        model = self.env[action.model_id.model]
        if ids and model.with_context(active_test=False).search_count([("id", "in", ids)]) != len(ids):
            raise invalid("unknown_record", "ids")
        records = model.browse(ids)
        records.check_access("write")
        return records

    def _check_action_rights(self, action):
        """Raise when Odoo does not give the user what ``action`` needs."""
        problem = self._action_rights_problem(action)
        if problem:
            model = action.wizard_model_id if problem == "no_wizard_access" else action.model_id
            raise invalid(problem, model=model.model, code="access_denied", message=MSG_WRITE)

    def _run_wizard(self, action, records, values, company):
        """Create the wizard of ``action`` for ``records`` with ``values``
        and press its buttons; return what the last one answered, the wizard
        and the names of the values given."""
        wizard = self._in_company(self.env[action.wizard_model_id.model], company).with_context(
            o3p_mcp_write=True, **self._wizard_context(action, records)
        )
        inputs = self._action_inputs(action)
        if values is None or values == {}:
            vals = {}
        elif not inputs:
            raise invalid("no_inputs", "values", message=MSG_VALUES)
        else:
            vals = self._write_values(wizard, values, inputs)
        self._check_required(wizard, [vals])
        record = wizard.create(vals)
        returned = None
        for step in action._step_names():
            returned = getattr(record, step)()
            # A button may answer with a wizard of the same kind that takes
            # over: the next button is pressed on that one.
            if (
                isinstance(returned, dict)
                and returned.get("res_model") == wizard._name
                and type(returned.get("res_id")) is int
                and returned["res_id"] != record.id
            ):
                successor = wizard.browse(returned["res_id"]).exists()
                if successor and successor.create_uid == self.env.user:
                    record = successor
        return returned, record, set(vals)

    def _form(self, wizard):
        """Return what the wizard shows after its last button: its readable
        fields, texts cut short. A review summary comes back here."""
        wizard = wizard.exists()
        if not wizard or not wizard.has_access("read"):
            return None
        names = [
            name
            for name, field in self._readable_fields(wizard).items()
            if name not in MAGIC_FIELDS
            and field.type in FORM_TYPES
            and (
                not field.relational
                or (
                    self._exposed_model(field.comodel_name)
                    and self.env[field.comodel_name].has_access("read")
                )
            )
        ]
        values = wizard.read(names)[0]
        return {
            name: value[:MAX_FORM_TEXT] if isinstance(value, str) else value
            for name, value in values.items()
            if name in names and value not in (False, None, "", [])
        }

    @contextmanager
    def _no_commit(self):
        """Refuse a commit or a rollback by the code an action runs.

        A call is committed together with its row in the call log and undone
        as a whole; code that ends the transaction itself would break both.
        """
        cr = self.env.cr
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(True)
            raise refused(MSG_COMMITS)

        saved = {name: vars(cr).get(name, _MISSING) for name in ("commit", "rollback")}
        cr.commit = cr.rollback = refuse
        try:
            yield
        except Exception as exc:
            # The code may have wrapped or swallowed the refusal.
            if attempts:
                raise refused(MSG_COMMITS) from exc
            raise
        finally:
            for name, value in saved.items():
                if value is _MISSING:
                    delattr(cr, name)
                else:
                    setattr(cr, name, value)
        if attempts:
            raise refused(MSG_COMMITS)

    def _returned(self, value):
        """Describe the action a button answered with: what Odoo would open
        next for the user. Only its kind, its name, a client action's tag, a
        notification's text and the model and record it points to, when the
        user may read them."""
        if not isinstance(value, dict):
            return None
        kind = value.get("type")
        if not isinstance(kind, str) or kind == "ir.actions.act_window_close":
            return None
        result = {"type": kind[:64]}
        name = value.get("name")
        if isinstance(name, str) and name:
            result["name"] = name[:MAX_NAME]
        if kind == "ir.actions.act_window":
            model = value.get("res_model")
            if isinstance(model, str) and model in self.env.registry and not self._model_denied(model):
                result["model"] = model
                if self.env[model]._transient:
                    result["wizard"] = True
                elif self._exposed_model(model) and self.env[model].has_access("read"):
                    res_id = value.get("res_id")
                    if type(res_id) is int and res_id > 0:
                        result["ids"] = self.env[model].browse(res_id).exists()._filtered_access("read").ids
        elif kind == "ir.actions.client":
            tag = value.get("tag")
            if isinstance(tag, str) and SAFE_NAME_RE.fullmatch(tag):
                result["tag"] = tag
            params = value.get("params")
            for key in ("title", "message"):
                text = params.get(key) if isinstance(params, dict) and tag == "display_notification" else None
                if isinstance(text, str) and text:
                    result[key] = text[:MAX_MESSAGE]
        return result

    def _acted(self, action, records, returned, form=None, names=()):
        """Return the answer and the call-log values of a run on ``records``."""
        ids = records.ids if records is not None else []
        existing = records.exists() if ids else []
        # A record the action moved out of the user's sight keeps its id only.
        readable = existing._filtered_access("read") if ids else []
        result = {
            "done": True,
            "records": [
                {"id": record.id, "display_name": record.display_name}
                if record in readable
                else {"id": record.id}
                for record in existing
            ],
        }
        if form:
            result["form"] = form
        described = self._returned(returned)
        if described:
            result["returned"] = described
        log = {}
        if ids:
            log["record_ids"] = ",".join(str(record_id) for record_id in ids)[:1000]
        if names:
            log["field_names"] = ",".join(sorted(names))[:1000]
        model = action.model_id.model or action.wizard_model_id.model
        return result, model, len(ids), log

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _op_actions(self, params):
        query = params.get("query")
        model = params.get("model")
        limit = params.get("limit", MAX_MODELS)
        if query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH):
            raise invalid("bad_value", "query")
        if model is not None and (not isinstance(model, str) or len(model) > MAX_QUERY_LENGTH):
            raise invalid("bad_value", "model")
        if type(limit) is not int or not 1 <= limit <= MAX_MODELS:
            raise invalid("bad_value", "limit", message=MSG_RANGE)
        offset = self._int_param(params.get("offset"), 0, 0, MAX_MODELS_OFFSET, "offset")
        needle = (query or "").strip().lower()
        matching = [
            entry
            for entry in map(self._action_entry, self._runnable_actions())
            if (model is None or entry["model"] == model)
            and (
                not needle
                or any(
                    needle in (entry[key] or "").lower()
                    for key in ("name", "title", "description", "model")
                )
            )
        ]
        page = matching[offset:offset + limit]
        result = {
            "actions": page,
            "total": len(matching),
            "truncated": offset + len(page) < len(matching),
        }
        return result, None, len(page)

    def _op_describe_action(self, params):
        action = self._action(params.get("action"))
        self._check_action_rights(action)
        steps = {"method": [action.method], "wizard": action._step_names()}
        result = {
            **self._action_entry(action),
            "inputs": self._input_descriptions(action),
            "summary": action.summary,
            "steps": steps.get(action.kind, []),
        }
        if action.kind == "wizard":
            result["wizard"] = action.wizard_model_id.model
        return result, action.model_id.model or action.wizard_model_id.model or None, 1

    def _op_run_action(self, params):
        action = self._action(params.get("action"))
        records = self._action_records(action, params.get("ids"))
        company, values = params.get("company"), params.get("values")
        if action.kind != "wizard" and values not in (None, {}):
            raise invalid("no_inputs", "values", message=MSG_VALUES)
        if records is not None:
            records = self._in_company(records, company).with_context(o3p_mcp_write=True)
        with self._no_commit():
            if action.kind == "wizard":
                returned, wizard, names = self._run_wizard(action, records, values, company)
                return self._acted(action, records, returned, self._form(wizard), names)
            if action.kind == "method":
                returned = getattr(records, action.method)()
            else:
                # As Odoo's web client runs it from the Action menu: Odoo
                # checks the action's groups and the write access again.
                server_action = self.env["ir.actions.server"].with_context(
                    records.env.context,
                    active_model=records._name,
                    active_ids=records.ids,
                    active_id=records.ids[0] if records else False,
                )
                returned = server_action.browse(action.server_action_id.id).run()
        return self._acted(action, records, returned)
