from contextlib import contextmanager

from odoo import models

from .api import MAX_MODELS, MAX_MODELS_OFFSET, MAX_QUERY_LENGTH, MSG_RANGE, invalid
from .api_write import MAX_MESSAGE, MSG_WRITE, refused
from .mcp_action import NAME_RE


MAX_NAME = 200
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

    def _action_for_user(self, action):
        """Tell whether the user is one of those ``action`` is exposed to."""
        return not action.group_ids or bool(action.group_ids & self.env.user.all_group_ids)

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
            groups = action.server_action_id.group_ids
            if groups and not groups & self.env.user.all_group_ids:
                return "no_write_access"
        return None

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
            "inputs": [],
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
        problem = self._action_rights_problem(action)
        if problem:
            raise invalid(
                problem, model=action.model_id.model, code="access_denied", message=MSG_WRITE
            )
        model = self.env[action.model_id.model]
        if ids and model.with_context(active_test=False).search_count([("id", "in", ids)]) != len(ids):
            raise invalid("unknown_record", "ids")
        records = model.browse(ids)
        records.check_access("write")
        return records

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
        next for the user. Only its kind, its name, a notification's text and
        the model and record it points to, when the user may read them."""
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
        elif kind == "ir.actions.client" and value.get("tag") == "display_notification":
            params = value.get("params")
            for key in ("title", "message"):
                text = params.get(key) if isinstance(params, dict) else None
                if isinstance(text, str) and text:
                    result[key] = text[:MAX_MESSAGE]
        return result

    def _acted(self, action, records, returned):
        """Return the answer and the call-log values of a run on ``records``."""
        existing = records.exists()
        readable = existing._filtered_access("read")
        result = {
            "done": True,
            "records": [
                {"id": record.id, "display_name": record.display_name}
                if record in readable
                else {"id": record.id}
                for record in existing
            ],
        }
        described = self._returned(returned)
        if described:
            result["returned"] = described
        log = {"record_ids": ",".join(str(record_id) for record_id in records.ids)[:1000]}
        return result, records._name, len(records), log

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
        problem = self._action_rights_problem(action)
        if problem:
            raise invalid(problem, model=action.model_id.model, code="access_denied")
        result = {
            **self._action_entry(action),
            "summary": action.summary,
            "steps": [action.method] if action.kind == "method" else [],
        }
        return result, action.model_id.model or None, 1

    def _op_run_action(self, params):
        action = self._action(params.get("action"))
        records = self._action_records(action, params.get("ids"))
        records = self._in_company(records, params.get("company")).with_context(o3p_mcp_write=True)
        with self._no_commit():
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
