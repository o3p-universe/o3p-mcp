import logging
import re

import psycopg2
from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain

from .denylist import (  # noqa: F401 - re-exported for callers of this module
    DEFAULT_DENIED_FIELDS,
    DEFAULT_DENIED_MODELS,
    READ_OVERRIDES_REVIEWED,
    field_denied,
    model_denied,
    overrides_read,
    patterns,
)
from .oauth import (
    SCOPE_ODOO_READ,
    SCOPE_ODOO_SQL,
    SCOPE_READ,
    find_by_secret,
    parse_scope,
    same_resource,
)


_logger = logging.getLogger(__name__)

DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LOG_DAYS = 30
MAX_MODELS = 50
MAX_QUERY_LENGTH = 200
MAX_SEARCH = 200
MAX_GROUPS = 500
MAX_GROUPBY = 5
MAX_AGGREGATES = 20
MAX_READ = 100
MAX_OFFSET = 1_000_000
DEFAULT_LIMIT = 80
MAX_FIELDS = 200
MAX_DOMAIN_ITEMS = 200
MAX_DOMAIN_DEPTH = 4
MAX_PATH_HOPS = 4
MAX_VALUES = 1000
MAX_STRING = 1000
FIELD_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")
ORDER_ITEM_RE = re.compile(r"([a-z_][a-z0-9_]*)(?: +(asc|desc))?", re.IGNORECASE)
GROUP_ORDER_ITEM_RE = re.compile(
    r"(__count|[a-z_][a-z0-9_]*(?::[a-z_]+)?)(?: +(asc|desc))?", re.IGNORECASE
)
GROUPBY_RE = re.compile(r"([a-z_][a-z0-9_]*)(?::([a-z_]+))?")
AGGREGATE_RE = re.compile(r"([a-z_][a-z0-9_]*):([a-z_]+)")
NUMERIC_TYPES = frozenset({"integer", "float", "monetary"})
NUMERIC_AGGREGATES = frozenset({"sum", "avg", "min", "max"})
COUNT_AGGREGATES = frozenset({"count", "count_distinct"})
DATE_GRANULARITIES = frozenset({"day", "week", "month", "quarter", "year"})
X2MANY_TYPES = frozenset({"one2many", "many2many"})
# Text operands on a relational field match the related records' display
# name: operator -> the positive operator applied to display_name.
NAME_OPERATORS = {
    "=": "=",
    "!=": "=",
    "in": "in",
    "not in": "in",
}
NEGATIVE_NAME_OPERATORS = frozenset({"!=", "not in", "not like", "not ilike"})
LIKE_OPERATORS = {
    "like": "like",
    "ilike": "ilike",
    "=like": "=like",
    "=ilike": "=ilike",
    "not like": "like",
    "not ilike": "ilike",
}

MSG_FIELDS = "A requested field is unknown, hidden or not readable."
MSG_DOMAIN = (
    "The domain is not valid, or uses a field that is unknown, hidden or not readable."
)
MSG_ORDER = "The order is not valid: use 'field' or 'field desc' items on sortable readable fields."
MSG_RANGE = "limit or offset is out of range."
MSG_IDS = "ids must be a list of 1 to 100 record ids."
MSG_KEYS = "The operation does not take one of the parameters."
MSG_GROUPBY = (
    "groupby is not valid: use groupable readable fields; date fields need a "
    "granularity (day, week, month, quarter, year, or hour for datetimes)."
)
MSG_AGGREGATES = (
    "aggregates are not valid: use __count, field:sum|avg|min|max on numeric "
    "readable fields, or field:count|count_distinct on stored readable fields."
)
MSG_GROUP_ORDER = "The order is not valid: use groupby or aggregate items with asc or desc."
# Domain operators accepted from clients. child_of and parent_of are not:
# Odoo walks the hierarchy as superuser, through records the user may not
# read, so the answer would reveal how hidden records are linked.
OPERATORS = frozenset(
    {
        "=", "!=", ">", ">=", "<", "<=", "=?", "in", "not in", "like", "not like",
        "ilike", "not ilike", "=like", "=ilike", "any", "not any",
    }
)

# Error code -> (HTTP status, default message). Messages are fixed texts:
# never an exception text, a parameter or record data.
ERRORS = {
    "invalid_params": (400, "The parameters are not valid."),
    "invalid_client": (401, "Client authentication failed."),
    "invalid_token": (403, "The access token is not valid."),
    "insufficient_scope": (403, "The access token does not allow this operation."),
    "access_denied": (403, "You are not allowed to read this."),
    "unknown_op": (404, "Unknown operation."),
    "unknown_model": (404, "Unknown model."),
    "result_too_large": (413, "The result is too large; ask for fewer records or fields."),
    "query_error": (422, "The query could not be run."),
    "too_many_requests": (429, "Too many queries are running; try again shortly."),
    "unavailable": (503, "SQL queries are not available."),
    "timeout": (504, "The query took too long."),
}


class ApiError(Exception):
    """A data API error with a fixed code and message.

    ``details`` is only used by the ``sql`` operation, for PostgreSQL's own
    answer about the user's query (see o3p.mcp.api ``_op_sql``); ``tables``
    is recorded in the call log, never returned.
    """

    def __init__(self, code, message=None, details=None, tables=None):
        super().__init__(code)
        self.code = code
        self.status, default = ERRORS[code]
        self.message = message or default
        self.details = details
        self.tables = tables

    def to_dict(self):
        error = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


# ``details`` of a validation error: why, as a fixed reason and hint, and
# where, as the field path or parameter name the client sent (schema names
# only, never record data or Odoo's exception text).
REASONS = {
    "unknown_param": "Remove the parameters this operation does not take.",
    "bad_value": "Check the type and the range of this parameter.",
    "bad_domain": "A domain is a list of [field, operator, value] conditions and the prefix operators &, | and !.",
    "too_deep": "Paths follow at most 4 fields and sub-domains nest at most 4 levels.",
    "unknown_field": "Call describe for the model's field names.",
    "not_readable": "The field is hidden or the user may not read it; describe lists the fields the user may use.",
    "not_filterable": "This model hides data when it is read: filter, sort and group only on id and its public fields.",
    "not_searchable": "The field is computed without a search method, so Odoo cannot filter on it; filter on fields describe marks searchable.",
    "not_sortable": "Sort on fields describe marks sortable.",
    "not_groupable": "Group on fields describe marks groupable.",
    "not_relational": "Only relational fields can be followed with a dot or with any / not any.",
    "bypass_relation": "This relation ignores the related model's access rules: search the related model first, then filter on its record ids.",
    "no_relation_access": "The user may not read the model this relation points to.",
    "no_model_access": "The user may not read this model.",
    "bad_operator": "Use =, !=, >, >=, <, <=, =?, in, not in, like, not like, ilike, not ilike, =like, =ilike, any or not any (child_of and parent_of are not available); on relations compare record ids, or match names with =, in, like or ilike.",
    "bad_order": "Use field, field asc or field desc items separated by commas.",
    "bad_aggregate": "Use __count, field:sum|avg|min|max on numeric fields, or field:count|count_distinct on stored fields.",
    "granularity_required": "Append :day, :week, :month, :quarter or :year to a date field (or :hour to a datetime field).",
    "bad_granularity": "Only date and datetime fields take a granularity: day, week, month, quarter or year, and hour for datetimes.",
}
# Hints that replace no_model_access for some models.
MODEL_HINTS = {
    "hr.employee": "The user may not read hr.employee: use hr.employee.public, which every employee may read.",
}
SAFE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:]{0,127}")


def invalid(reason, field=None, *, message=None, model=None, code="invalid_params"):
    """Return an ApiError whose details say why (reason, hint) and where.

    ``field`` is echoed only when it looks like a field path or a parameter
    name; ``model`` only when it is a model name.
    """
    details = {}
    if isinstance(field, str) and SAFE_NAME_RE.fullmatch(field):
        details["field"] = field
    if isinstance(model, str) and SAFE_NAME_RE.fullmatch(model):
        details["model"] = model
    details["reason"] = reason
    details["hint"] = (reason == "no_model_access" and MODEL_HINTS.get(model)) or REASONS[reason]
    return ApiError(code, message, details=details)


class O3pMcpApi(models.AbstractModel):
    _name = "o3p.mcp.api"
    _description = "MCP Data API"

    # Operation -> (method, scope, parameters). The sql operation lives in
    # api_sql.py.
    _OPS = {
        "models": ("_op_models", SCOPE_ODOO_READ, {"query", "limit"}),
        "describe": ("_op_describe", SCOPE_ODOO_READ, {"model"}),
        "search": (
            "_op_search",
            SCOPE_ODOO_READ,
            {"model", "domain", "fields", "order", "limit", "offset"},
        ),
        "read": ("_op_read", SCOPE_ODOO_READ, {"model", "ids", "fields"}),
        "group": (
            "_op_group",
            SCOPE_ODOO_READ,
            {"model", "domain", "groupby", "aggregates", "order", "limit"},
        ),
        "sql": ("_op_sql", SCOPE_ODOO_SQL, {"query", "limit"}),
    }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.model
    def _setting(self, key, default):
        params = self.env["ir.config_parameter"].sudo()
        if isinstance(default, int):
            value = params.get_int(key, default)
            return value if value > 0 else default
        return params.get_str(key, "")

    @api.model
    def _denied_model_patterns(self):
        return patterns(
            self._setting("o3p_mcp.api_denied_models", ""), DEFAULT_DENIED_MODELS
        )

    @api.model
    def _denied_field_patterns(self):
        return patterns(
            self._setting("o3p_mcp.api_denied_fields", ""), DEFAULT_DENIED_FIELDS
        )

    @api.model
    def _max_response_bytes(self):
        return self._setting("o3p_mcp.api_max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)

    @api.model
    def _timeout_seconds(self):
        return self._setting("o3p_mcp.api_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------
    # Authentication of the subject token
    # ------------------------------------------------------------------

    @api.model
    def _op_scope(self, op):
        if op not in self._OPS:
            raise ApiError("unknown_op")
        return self._OPS[op][1]

    @api.model
    def _check_token(self, value, scope):
        """Return the token and user behind ``value`` if it allows ``scope``.

        The token must be unrevoked and unexpired, issued for the configured
        MCP URL to an active client, and its user active, internal and still
        allowed ``okf.read`` by the MCP groups. Nothing is written.
        """
        token = find_by_secret(self.env["o3p.mcp.token"], "access_hash", value)
        resource_url = self.env["o3p.mcp.oauth"]._resource_url()
        if (
            not token
            or token.revoked_at
            or token.expires_at <= fields.Datetime.now()
            or not token.client_id.active
            or not resource_url
            or not same_resource(token.resource or "", resource_url)
        ):
            raise ApiError("invalid_token")
        user = token.user_id
        scopes = parse_scope(token.scope) & self.env["o3p.mcp.oauth"]._scopes_for_user(user)
        if SCOPE_READ not in scopes:
            raise ApiError("invalid_token")
        if scope not in scopes:
            raise ApiError("insufficient_scope")
        return token, user

    # ------------------------------------------------------------------
    # Running an operation as the user
    # ------------------------------------------------------------------

    def _run(self, op, params):
        """Run ``op`` as the current user; return ``(result, model, rows)``,
        plus the tables read for ``sql``.

        The operation runs in a savepoint switched to a read-only transaction
        with a statement timeout, and the savepoint is always rolled back:
        PostgreSQL itself refuses any write. Errors raised by the ORM or the
        database become fixed API errors; their text never leaves the server.
        """
        if not isinstance(params, dict):
            raise invalid("bad_value", "params")
        method_name, _scope, keys = self._OPS[op]
        unknown = sorted(set(params) - keys, key=str)
        if unknown:
            raise invalid("unknown_param", unknown[0], message=MSG_KEYS)
        method = getattr(self, method_name)
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("SAVEPOINT o3p_mcp_api")
        try:
            cr.execute("SET LOCAL transaction_read_only = on")
            cr.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [f"{self._timeout_seconds() * 1000}ms"],
            )
            return method(params)
        except ApiError:
            raise
        except AccessError as exc:
            raise ApiError("access_denied") from exc
        except pg_errors.QueryCanceled as exc:
            raise ApiError("timeout") from exc
        except (
            psycopg2.Error,
            UserError,
            ValidationError,
            ValueError,
            TypeError,
            KeyError,
            AssertionError,
        ) as exc:
            _logger.info("MCP data API %s failed: %s", op, type(exc).__name__)
            raise ApiError("query_error") from exc
        except Exception as exc:  # noqa: BLE001 - nothing unexpected reaches the client
            _logger.warning("MCP data API %s failed unexpectedly: %s", op, type(exc).__name__)
            raise ApiError("query_error") from exc
        finally:
            # Also restores read/write mode and the previous statement timeout.
            cr.execute("ROLLBACK TO SAVEPOINT o3p_mcp_api")
            cr.execute("RELEASE SAVEPOINT o3p_mcp_api")
            self.env.invalidate_all(flush=False)

    # ------------------------------------------------------------------
    # Models and fields the user may see
    # ------------------------------------------------------------------

    def _model_denied(self, name):
        """Tell whether model ``name`` is hidden (see models/denylist.py)."""
        return model_denied(name, self._denied_model_patterns())

    def _field_denied(self, field):
        """Tell whether ``field`` is hidden (see models/denylist.py)."""
        return field_denied(
            field, self._denied_field_patterns(), self._denied_model_patterns()
        )

    def _exposed_model(self, name):
        """Tell whether model ``name`` may appear at all, whatever the user."""
        if not isinstance(name, str) or name not in self.env.registry:
            return False
        model = self.env[name]
        return not (model._abstract or model._transient or self._model_denied(name))

    def _model(self, name):
        """Return model ``name`` for the user, or raise."""
        if not self._exposed_model(name):
            raise ApiError("unknown_model")
        model = self.env[name]
        if not model.has_access("read"):
            raise invalid("no_model_access", model=name, code="access_denied")
        return model

    def _readable_fields(self, model):
        """Return the fields of ``model`` the user may read and use."""
        return {
            name: field
            for name, field in model._fields.items()
            if model.has_field_access(field, "read") and not self._field_denied(field)
        }

    # ------------------------------------------------------------------
    # Parameter validation
    # ------------------------------------------------------------------

    @staticmethod
    def _int_param(value, default, low, high, name):
        if value is None:
            return default
        if type(value) is not int or not low <= value <= high:
            raise invalid("bad_value", name, message=MSG_RANGE)
        return value

    def _field_problem(self, model, name, path, message):
        """Return the error for ``name`` missing from the usable fields."""
        if name not in model._fields:
            reason = "unknown_field"
        elif name not in self._readable_fields(model):
            reason = "not_readable"
        else:
            reason = "not_filterable"
        return invalid(reason, path, message=message)

    def _fields_param(self, model, value):
        """Return the requested field names, all readable and not hidden."""
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_FIELDS
            or not all(isinstance(name, str) for name in value)
        ):
            raise invalid("bad_value", "fields", message=MSG_FIELDS)
        readable = self._readable_fields(model)
        for name in value:
            if name not in readable:
                reason = "unknown_field" if name not in model._fields else "not_readable"
                raise invalid(reason, name, message=MSG_FIELDS)
        return list(dict.fromkeys(value))

    def _comodel(self, model, field, path):
        """Return the model ``field`` relates to, as the user, or raise.

        The related model must be exposed and searched with its own access
        rules; the user must be allowed to read it.
        """
        if not field.relational:
            raise invalid("not_relational", path, message=MSG_DOMAIN)
        if field.bypass_search_access:
            raise invalid("bypass_relation", path, message=MSG_DOMAIN)
        if not self._exposed_model(field.comodel_name):
            raise invalid("not_readable", path, message=MSG_DOMAIN)
        comodel = model.env[field.comodel_name]
        if not comodel.has_access("read"):
            raise invalid("no_relation_access", path, code="access_denied")
        return comodel

    @staticmethod
    def _check_operand(value, path):
        scalars = (str, int, float, bool, type(None))
        if isinstance(value, list):
            if len(value) > MAX_VALUES or not all(isinstance(item, scalars) for item in value):
                raise invalid("bad_value", path, message=MSG_DOMAIN)
            values = value
        elif isinstance(value, scalars):
            values = [value]
        else:
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        if any(isinstance(item, str) and len(item) > MAX_STRING for item in values):
            raise invalid("bad_value", path, message=MSG_DOMAIN)

    def _x2many_term(self, comodel, name, operator, operand, path):
        """Rewrite a condition on a one2many or many2many field with ``any``.

        Odoo applies the related model's record rules to ``any`` / ``not any``
        but not to the short forms (``in`` ids, ``=`` id, ``!= False``…), which
        would reveal hidden related records. Forms without a faithful
        rewrite (comparisons, ``=?``) are refused.
        """
        if operator in ("in", "not in"):
            ids = operand if isinstance(operand, list) else [operand]
            if len(ids) > MAX_VALUES or not all(type(item) is int and item > 0 for item in ids):
                raise invalid("bad_value", path, message=MSG_DOMAIN)
            return [name, "any" if operator == "in" else "not any", [["id", "in", ids]]]
        if operator in ("=", "!="):
            if operand is False or operand is None:
                return [name, "not any" if operator == "=" else "any", []]
            if type(operand) is int and operand > 0:
                return [name, "any" if operator == "=" else "not any", [["id", "=", operand]]]
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        if operator in LIKE_OPERATORS:
            if not isinstance(operand, str):
                raise invalid("bad_value", path, message=MSG_DOMAIN)
            return self._display_name_term(comodel, name, operator, operand, path)
        raise invalid("bad_operator", path, message=MSG_DOMAIN)

    def _display_name_term(self, comodel, name, operator, operand, path):
        """Match the related records of field ``name`` by display name.

        The related model's record rules apply through ``any``; the display
        name must be filterable on it (a model hiding data on read does not
        allow it).
        """
        values = operand if isinstance(operand, list) else [operand]
        if len(values) > MAX_VALUES or any(len(value) > MAX_STRING for value in values):
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        if "display_name" not in self._filterable_fields(comodel):
            raise invalid("not_filterable", path, message=MSG_DOMAIN)
        positive = LIKE_OPERATORS.get(operator) or NAME_OPERATORS[operator]
        return [
            name,
            "not any" if operator in NEGATIVE_NAME_OPERATORS else "any",
            [["display_name", positive, operand]],
        ]

    def _many2one_term(self, model, field, operator, operand, path):
        """Return a condition on a many2one field.

        Record ids (and ``false``) are compared with the column itself. Text
        operands match the related records by display name through
        ``_display_name_term``, so the related model's rules apply; Odoo
        would otherwise search names without them where the field bypasses
        access. A relation that bypasses access is refused by ``_comodel``.
        """
        values = operand if isinstance(operand, list) else [operand]
        if len(values) > MAX_VALUES:
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        if all(
            value is False or value is None or (type(value) is int and value > 0)
            for value in values
        ):
            if operator in LIKE_OPERATORS:
                raise invalid("bad_value", path, message=MSG_DOMAIN)
            return [field.name, operator, operand]
        if not all(isinstance(value, str) for value in values):
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        list_operator = operator in ("in", "not in")
        if list_operator != isinstance(operand, list):
            raise invalid("bad_value", path, message=MSG_DOMAIN)
        if not (operator in LIKE_OPERATORS or operator in NAME_OPERATORS):
            raise invalid("bad_operator", path, message=MSG_DOMAIN)
        comodel = self._comodel(model, field, path)
        return self._display_name_term(comodel, field.name, operator, operand, path)

    def _term(self, model, names, operator, operand, depth, prefix=""):
        """Return the checked domain term for the field path ``names``.

        Every hop must be a readable, non-hidden field. A relational hop
        becomes an ``any`` sub-domain, as Odoo itself expands paths, so the
        related model's record rules apply; conditions on one2many and
        many2many fields are rewritten by ``_x2many_term``.
        """
        path = prefix + names[0]
        field = self._filterable_fields(model).get(names[0])
        if field is None:
            raise self._field_problem(model, names[0], path, MSG_DOMAIN)
        # Odoo silently ignores a condition on a computed field it cannot
        # search (not stored, no search method): the answer would be wrong.
        if not field._description_searchable:
            raise invalid("not_searchable", path, message=MSG_DOMAIN)
        if len(names) > 1:
            comodel = self._comodel(model, field, path)
            return [
                field.name,
                "any",
                [self._term(comodel, names[1:], operator, operand, depth, f"{path}.")],
            ]
        if operator in ("any", "not any"):
            comodel = self._comodel(model, field, path)
            return [
                field.name,
                operator,
                self._domain_terms(comodel, operand, depth + 1, f"{path}."),
            ]
        if field.type in X2MANY_TYPES:
            comodel = self._comodel(model, field, path)
            return self._x2many_term(comodel, field.name, operator, operand, path)
        if field.type == "many2one":
            return self._many2one_term(model, field, operator, operand, path)
        self._check_operand(operand, path)
        return [field.name, operator, operand]

    def _domain_terms(self, model, value, depth=0, prefix=""):
        """Return the checked and rewritten terms of the domain ``value``."""
        where = prefix.rstrip(".") or "domain"
        if value is None:
            value = []
        if depth > MAX_DOMAIN_DEPTH:
            raise invalid("too_deep", where, message=MSG_DOMAIN)
        if not isinstance(value, list) or len(value) > MAX_DOMAIN_ITEMS:
            raise invalid("bad_domain", where, message=MSG_DOMAIN)
        terms = []
        for item in value:
            if item in ("&", "|", "!"):
                terms.append(item)
                continue
            if not isinstance(item, list) or len(item) != 3:
                raise invalid("bad_domain", where, message=MSG_DOMAIN)
            path, operator, operand = item
            names = path.split(".") if isinstance(path, str) else []
            if not names or not all(FIELD_NAME_RE.fullmatch(name) for name in names):
                raise invalid("bad_domain", where, message=MSG_DOMAIN)
            if len(names) > MAX_PATH_HOPS:
                raise invalid("too_deep", prefix + path, message=MSG_DOMAIN)
            if not isinstance(operator, str) or operator not in OPERATORS:
                raise invalid("bad_operator", prefix + path, message=MSG_DOMAIN)
            terms.append(self._term(model, names, operator, operand, depth, prefix))
        return terms

    def _domain_param(self, model, value):
        """Return ``value`` as a domain checked and rewritten as the user.

        The structure is then parsed by Odoo's own domain machinery.
        """
        terms = self._domain_terms(model, value)
        try:
            return Domain(terms)
        except (ValueError, TypeError) as exc:
            raise invalid("bad_domain", "domain", message=MSG_DOMAIN) from exc

    def _order_param(self, model, value):
        """Return a normalised ``field [asc|desc], …`` order, or None."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_STRING:
            raise invalid("bad_order", "order", message=MSG_ORDER)
        filterable = self._filterable_fields(model)
        items = []
        for item in value.split(","):
            match = ORDER_ITEM_RE.fullmatch(item.strip())
            if not match:
                raise invalid("bad_order", "order", message=MSG_ORDER)
            name = match.group(1)
            field = filterable.get(name)
            if field is None:
                raise self._field_problem(model, name, name, MSG_ORDER)
            if not field._description_sortable(model.env):
                raise invalid("not_sortable", name, message=MSG_ORDER)
            items.append(f"{name} {(match.group(2) or 'asc').lower()}")
        return ", ".join(items)

    def _groupby_param(self, model, value):
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_GROUPBY
            or not all(isinstance(spec, str) for spec in value)
        ):
            raise invalid("bad_value", "groupby", message=MSG_GROUPBY)
        filterable = self._filterable_fields(model)
        for spec in value:
            match = GROUPBY_RE.fullmatch(spec)
            if not match:
                raise invalid("bad_value", "groupby", message=MSG_GROUPBY)
            name, granularity = match.group(1), match.group(2)
            field = filterable.get(name)
            if field is None:
                raise self._field_problem(model, name, spec, MSG_GROUPBY)
            if not field._description_groupable(model.env):
                raise invalid("not_groupable", spec, message=MSG_GROUPBY)
            if field.type in ("date", "datetime"):
                allowed = DATE_GRANULARITIES | ({"hour"} if field.type == "datetime" else set())
                if not granularity:
                    raise invalid("granularity_required", spec, message=MSG_GROUPBY)
                if granularity not in allowed:
                    raise invalid("bad_granularity", spec, message=MSG_GROUPBY)
            elif granularity:
                raise invalid("bad_granularity", spec, message=MSG_GROUPBY)
        return list(dict.fromkeys(value))

    def _aggregates_param(self, model, value):
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_AGGREGATES
            or not all(isinstance(spec, str) for spec in value)
        ):
            raise invalid("bad_value", "aggregates", message=MSG_AGGREGATES)
        filterable = self._filterable_fields(model)
        for spec in value:
            if spec == "__count":
                continue
            match = AGGREGATE_RE.fullmatch(spec)
            if not match:
                raise invalid("bad_aggregate", "aggregates", message=MSG_AGGREGATES)
            field = filterable.get(match.group(1))
            if field is None:
                raise self._field_problem(model, match.group(1), spec, MSG_AGGREGATES)
            function = match.group(2)
            if not field.store or not (
                function in COUNT_AGGREGATES
                or (function in NUMERIC_AGGREGATES and field.type in NUMERIC_TYPES)
            ):
                raise invalid("bad_aggregate", spec, message=MSG_AGGREGATES)
        return list(dict.fromkeys(value))

    @staticmethod
    def _group_order_param(value, keys):
        """Return an order on groupby or aggregate items, or None."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_STRING:
            raise invalid("bad_order", "order", message=MSG_GROUP_ORDER)
        items = []
        for item in value.split(","):
            match = GROUP_ORDER_ITEM_RE.fullmatch(item.strip())
            if not match or match.group(1) not in keys:
                raise invalid("bad_order", "order", message=MSG_GROUP_ORDER)
            items.append(f"{match.group(1)} {(match.group(2) or 'asc').lower()}")
        return ", ".join(items)

    @api.model
    @api.ormcache("name")
    def _model_hides_on_read(self, name):
        """Tell whether model ``name`` hides data in a read override."""
        return name not in READ_OVERRIDES_REVIEWED and overrides_read(type(self.env[name]))

    def _filterable_fields(self, model):
        """Return the fields of ``model`` the user may filter, sort and group on.

        These are the readable fields, except for a model that hides data in
        a read override: only ``id`` and the fields it declares public.
        """
        readable = self._readable_fields(model)
        if not self._model_hides_on_read(model._name):
            return readable
        public = {"id"}
        if hasattr(model, "_get_public_fields"):
            public |= set(model._get_public_fields())
        return {name: field for name, field in readable.items() if name in public}

    def _model_labels(self, names):
        records = (
            self.env["ir.model"]
            .sudo()
            .with_context(lang=self.env.lang)
            .search([("model", "in", list(names))])
        )
        return {record.model: record.name for record in records}

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _op_models(self, params):
        query = params.get("query")
        limit = params.get("limit", MAX_MODELS)
        if query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH):
            raise invalid("bad_value", "query")
        if type(limit) is not int or not 1 <= limit <= MAX_MODELS:
            raise invalid("bad_value", "limit", message=MSG_RANGE)
        names = [
            name
            for name in sorted(self.env.registry)
            if self._exposed_model(name) and self.env[name].has_access("read")
        ]
        labels = self._model_labels(names)
        needle = (query or "").strip().lower()
        result = [
            {"model": name, "name": labels.get(name) or self.env[name]._description or name}
            for name in names
            if not needle
            or needle in name.lower()
            or needle in (labels.get(name) or "").lower()
        ][:limit]
        return result, None, len(result)

    def _op_describe(self, params):
        model = self._model(params.get("model"))
        readable = self._readable_fields(model)
        descriptions = model.fields_get(
            list(readable),
            attributes=["string", "type", "relation", "required", "readonly", "selection", "help"],
        )
        result_fields = []
        for name in sorted(descriptions):
            description = descriptions[name]
            item = {
                "name": name,
                "string": description.get("string") or name,
                "type": description.get("type"),
                "required": bool(description.get("required")),
                "readonly": bool(description.get("readonly")),
            }
            if description.get("relation"):
                item["relation"] = description["relation"]
            if isinstance(description.get("selection"), (list, tuple)):
                item["selection"] = [[key, label] for key, label in description["selection"]]
            if description.get("help"):
                item["help"] = description["help"]
            result_fields.append(item)
        result = {
            "model": model._name,
            "name": self._model_labels([model._name]).get(model._name) or model._description,
            "table": self._table_name(model),
            "fields": result_fields,
        }
        return result, model._name, len(result_fields)

    def _table_name(self, model):
        """Return the table or view that stores ``model``, or None.

        Models computed from a query have a ``_table`` name but no such
        relation in the database.
        """
        self.env.cr.execute("SELECT to_regclass(%s) IS NOT NULL", [f'"{model._table}"'])
        return model._table if self.env.cr.fetchone()[0] else None

    def _op_search(self, params):
        model = self._model(params.get("model"))
        domain = self._domain_param(model, params.get("domain"))
        names = self._fields_param(model, params.get("fields"))
        order = self._order_param(model, params.get("order"))
        limit = self._int_param(params.get("limit"), DEFAULT_LIMIT, 1, MAX_SEARCH, "limit")
        offset = self._int_param(params.get("offset"), 0, 0, MAX_OFFSET, "offset")
        records = model.search_fetch(domain, names, offset=offset, limit=limit, order=order)
        rows = records.read(names)
        count = model.search_count(domain)
        result = {"records": rows, "count": count, "truncated": offset + len(rows) < count}
        return result, model._name, len(rows)

    def _op_read(self, params):
        model = self._model(params.get("model"))
        ids = params.get("ids")
        if (
            not isinstance(ids, list)
            or not 0 < len(ids) <= MAX_READ
            or not all(type(value) is int and value > 0 for value in ids)
        ):
            raise invalid("bad_value", "ids", message=MSG_IDS)
        names = self._fields_param(model, params.get("fields"))
        # Records that do not exist and records the user may not read are left
        # out alike, so the answer does not tell whether a hidden record exists.
        records = model.browse(list(dict.fromkeys(ids))).exists()._filtered_access("read")
        rows = records.read(names)
        return {"records": rows}, model._name, len(rows)

    def _op_group(self, params):
        model = self._model(params.get("model"))
        domain = self._domain_param(model, params.get("domain"))
        groupby = self._groupby_param(model, params.get("groupby"))
        aggregates = self._aggregates_param(model, params.get("aggregates"))
        order = self._group_order_param(params.get("order"), {*groupby, *aggregates})
        limit = self._int_param(params.get("limit"), DEFAULT_LIMIT, 1, MAX_GROUPS, "limit")
        # No group expansion or temporal filling: every group comes from the
        # records the domain finds for the user.
        groups = model.with_context(read_group_expand=False, fill_temporal=False).formatted_read_group(
            domain, groupby, aggregates, order=order, limit=limit
        )
        keys = (*groupby, *aggregates)
        result = [{key: group[key] for key in keys} for group in groups]
        return {"groups": result}, model._name, len(result)
