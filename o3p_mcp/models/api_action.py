from odoo import models

from .api import MAX_MODELS, MAX_MODELS_OFFSET, MAX_QUERY_LENGTH, MSG_RANGE, invalid
from .mcp_action import NAME_RE


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
