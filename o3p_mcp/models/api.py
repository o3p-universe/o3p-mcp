import datetime
import fnmatch
import logging
import re
from zoneinfo import ZoneInfo, available_timezones

import babel
import babel.dates

import psycopg2
from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.models import READ_GROUP_DISPLAY_FORMAT, READ_GROUP_TIME_GRANULARITY
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT, date_utils, get_lang

from .oauth import (
    SCOPE_ODOO_READ,
    SCOPE_READ,
    find_by_secret,
    get_config,
    get_config_int,
    parse_scope,
    same_resource,
)


_logger = logging.getLogger(__name__)

DEFAULT_DENIED_MODELS = (
    "ir.config_parameter",
    "res.users.apikeys*",
    "o3p.mcp.*",
    "auth_totp*",
    "ir.mail_server",
    "fetchmail.server",
    "payment.provider",
)
DEFAULT_DENIED_FIELDS = ("*password*", "*secret*", "*token*", "*api_key*")
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
TIMEZONES = frozenset(available_timezones())
X2MANY_TYPES = frozenset({"one2many", "many2many"})
# Like operators on an x2many field match the related records' display name.
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
# Domain operators accepted from clients.
OPERATORS = frozenset(
    {
        "=", "!=", ">", ">=", "<", "<=", "=?", "in", "not in", "like", "not like",
        "ilike", "not ilike", "=like", "=ilike", "child_of", "parent_of", "any",
        "not any",
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
    "timeout": (504, "The query took too long."),
}


class ApiError(Exception):
    """A data API error with a fixed code and message."""

    def __init__(self, code, message=None):
        super().__init__(code)
        self.code = code
        self.status, default = ERRORS[code]
        self.message = message or default

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message}}


def patterns(value, default):
    """Return the fnmatch patterns of a setting: one per line or comma."""
    items = [item.strip() for item in (value or "").replace(",", "\n").splitlines()]
    return tuple(item for item in items if item) or default


class O3pMcpApi(models.AbstractModel):
    _name = "o3p.mcp.api"
    _description = "MCP Data API"

    # Operation -> (method, scope, parameters). The sql operation is not
    # offered yet.
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
    }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.model
    def _setting(self, key, default):
        if isinstance(default, int):
            value = get_config_int(self.env, key, default)
            return value if value > 0 else default
        return get_config(self.env, key)

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
        """Run ``op`` as the current user; return ``(result, model, rows)``.

        The operation runs in a savepoint switched to a read-only transaction
        with a statement timeout, and the savepoint is always rolled back:
        PostgreSQL itself refuses any write. Errors raised by the ORM or the
        database become fixed API errors; their text never leaves the server.
        """
        if not isinstance(params, dict):
            raise ApiError("invalid_params")
        method_name, _scope, keys = self._OPS[op]
        if not set(params) <= keys:
            raise ApiError("invalid_params", MSG_KEYS)
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
        finally:
            # Also restores read/write mode and the previous statement timeout.
            cr.execute("ROLLBACK TO SAVEPOINT o3p_mcp_api")
            cr.execute("RELEASE SAVEPOINT o3p_mcp_api")
            self.env.invalidate_all(flush=False)

    # ------------------------------------------------------------------
    # Models and fields the user may see
    # ------------------------------------------------------------------

    def _model_denied(self, name):
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self._denied_model_patterns())

    def _field_denied(self, field):
        name = field.name.lower()
        if field.type == "binary" or any(
            fnmatch.fnmatchcase(name, pattern.lower())
            for pattern in self._denied_field_patterns()
        ):
            return True
        return bool(field.relational and self._model_denied(field.comodel_name))

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
            raise ApiError("access_denied")
        return model

    def _readable_fields(self, model):
        """Return the fields of ``model`` the user may read and use."""
        return {
            name: field
            for name, field in model._fields.items()
            if field.is_accessible(model.env) and not self._field_denied(field)
        }

    # ------------------------------------------------------------------
    # Parameter validation
    # ------------------------------------------------------------------

    @staticmethod
    def _int_param(value, default, low, high):
        if value is None:
            return default
        if type(value) is not int or not low <= value <= high:
            raise ApiError("invalid_params", MSG_RANGE)
        return value

    def _fields_param(self, model, value):
        """Return the requested field names, all readable and not hidden."""
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_FIELDS
            or not all(isinstance(name, str) for name in value)
        ):
            raise ApiError("invalid_params", MSG_FIELDS)
        readable = self._readable_fields(model)
        if any(name not in readable for name in value):
            raise ApiError("invalid_params", MSG_FIELDS)
        return list(dict.fromkeys(value))

    def _comodel(self, model, field):
        """Return the model ``field`` relates to, as the user, or raise.

        The related model must be exposed and searched with its own access
        rules; the user must be allowed to read it.
        """
        if (
            not field.relational
            or field.auto_join
            or not self._exposed_model(field.comodel_name)
        ):
            raise ApiError("invalid_params", MSG_DOMAIN)
        comodel = model.env[field.comodel_name]
        if not comodel.has_access("read"):
            raise ApiError("access_denied")
        return comodel

    @staticmethod
    def _check_operand(value):
        scalars = (str, int, float, bool, type(None))
        if isinstance(value, list):
            if len(value) > MAX_VALUES or not all(isinstance(item, scalars) for item in value):
                raise ApiError("invalid_params", MSG_DOMAIN)
            values = value
        elif isinstance(value, scalars):
            values = [value]
        else:
            raise ApiError("invalid_params", MSG_DOMAIN)
        if any(isinstance(item, str) and len(item) > MAX_STRING for item in values):
            raise ApiError("invalid_params", MSG_DOMAIN)

    @staticmethod
    def _x2many_term(name, operator, operand):
        """Rewrite a condition on a one2many or many2many field with ``any``.

        Odoo applies the related model's record rules to ``any`` / ``not any``
        but not to the short forms (``in`` ids, ``=`` id, ``!= False``…), which
        would reveal hidden related records. Forms without a faithful
        rewrite (``child_of``, ``parent_of``, comparisons, ``=?``) are refused.
        """
        if operator in ("in", "not in"):
            ids = operand if isinstance(operand, list) else [operand]
            if len(ids) > MAX_VALUES or not all(type(item) is int and item > 0 for item in ids):
                raise ApiError("invalid_params", MSG_DOMAIN)
            return [name, "any" if operator == "in" else "not any", [["id", "in", ids]]]
        if operator in ("=", "!="):
            if operand is False or operand is None:
                return [name, "not any" if operator == "=" else "any", []]
            if type(operand) is int and operand > 0:
                return [name, "any" if operator == "=" else "not any", [["id", "=", operand]]]
        if operator in LIKE_OPERATORS and isinstance(operand, str) and len(operand) <= MAX_STRING:
            return [
                name,
                "not any" if operator.startswith("not ") else "any",
                [["display_name", LIKE_OPERATORS[operator], operand]],
            ]
        raise ApiError("invalid_params", MSG_DOMAIN)

    def _term(self, model, names, operator, operand, depth):
        """Return the checked domain term for the field path ``names``.

        Every hop must be a readable, non-hidden field. A relational hop
        becomes an ``any`` sub-domain, as Odoo itself expands paths, so the
        related model's record rules apply; conditions on one2many and
        many2many fields are rewritten by ``_x2many_term``.
        """
        field = self._readable_fields(model).get(names[0])
        if field is None:
            raise ApiError("invalid_params", MSG_DOMAIN)
        if len(names) > 1:
            comodel = self._comodel(model, field)
            return [field.name, "any", [self._term(comodel, names[1:], operator, operand, depth)]]
        if operator in ("any", "not any"):
            comodel = self._comodel(model, field)
            return [field.name, operator, self._domain_terms(comodel, operand, depth + 1)]
        if field.type in X2MANY_TYPES:
            self._comodel(model, field)
            return self._x2many_term(field.name, operator, operand)
        self._check_operand(operand)
        return [field.name, operator, operand]

    def _domain_terms(self, model, value, depth=0):
        """Return the checked and rewritten terms of the domain ``value``."""
        if value is None:
            value = []
        if (
            not isinstance(value, list)
            or len(value) > MAX_DOMAIN_ITEMS
            or depth > MAX_DOMAIN_DEPTH
        ):
            raise ApiError("invalid_params", MSG_DOMAIN)
        terms = []
        for item in value:
            if item in ("&", "|", "!"):
                terms.append(item)
                continue
            if not isinstance(item, list) or len(item) != 3:
                raise ApiError("invalid_params", MSG_DOMAIN)
            path, operator, operand = item
            names = path.split(".") if isinstance(path, str) else []
            if (
                not isinstance(operator, str)
                or operator not in OPERATORS
                or not 0 < len(names) <= MAX_PATH_HOPS
                or not all(FIELD_NAME_RE.fullmatch(name) for name in names)
            ):
                raise ApiError("invalid_params", MSG_DOMAIN)
            terms.append(self._term(model, names, operator, operand, depth))
        return terms

    def _domain_param(self, model, value):
        """Return ``value`` as a domain checked and rewritten as the user."""
        terms = self._domain_terms(model, value)
        # Prefix notation: every "&" and "|" needs two operands, "!" one; the
        # remaining terms are joined with an implicit "&".
        operands = 0
        for item in reversed(terms):
            if item in ("&", "|"):
                if operands < 2:
                    raise ApiError("invalid_params", MSG_DOMAIN)
                operands -= 1
            elif item == "!":
                if operands < 1:
                    raise ApiError("invalid_params", MSG_DOMAIN)
            else:
                operands += 1
        return terms

    def _order_param(self, model, value):
        """Return a normalised ``field [asc|desc], …`` order, or None."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_STRING:
            raise ApiError("invalid_params", MSG_ORDER)
        readable = self._readable_fields(model)
        items = []
        for item in value.split(","):
            match = ORDER_ITEM_RE.fullmatch(item.strip())
            field = match and readable.get(match.group(1))
            if not field or not field._description_sortable(model.env):
                raise ApiError("invalid_params", MSG_ORDER)
            items.append(f"{match.group(1)} {(match.group(2) or 'asc').lower()}")
        return ", ".join(items)

    def _groupby_param(self, model, value):
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_GROUPBY
            or not all(isinstance(spec, str) for spec in value)
        ):
            raise ApiError("invalid_params", MSG_GROUPBY)
        readable = self._readable_fields(model)
        for spec in value:
            match = GROUPBY_RE.fullmatch(spec)
            field = match and readable.get(match.group(1))
            if not field or not field._description_groupable(model.env):
                raise ApiError("invalid_params", MSG_GROUPBY)
            granularity = match.group(2)
            if field.type in ("date", "datetime"):
                allowed = DATE_GRANULARITIES | ({"hour"} if field.type == "datetime" else set())
                if granularity not in allowed:
                    raise ApiError("invalid_params", MSG_GROUPBY)
            elif granularity:
                raise ApiError("invalid_params", MSG_GROUPBY)
        return list(dict.fromkeys(value))

    def _aggregates_param(self, model, value):
        if (
            not isinstance(value, list)
            or not 0 < len(value) <= MAX_AGGREGATES
            or not all(isinstance(spec, str) for spec in value)
        ):
            raise ApiError("invalid_params", MSG_AGGREGATES)
        readable = self._readable_fields(model)
        for spec in value:
            if spec == "__count":
                continue
            match = AGGREGATE_RE.fullmatch(spec)
            field = match and readable.get(match.group(1))
            if not field or not field.store:
                raise ApiError("invalid_params", MSG_AGGREGATES)
            function = match.group(2)
            if not (
                function in COUNT_AGGREGATES
                or (function in NUMERIC_AGGREGATES and field.type in NUMERIC_TYPES)
            ):
                raise ApiError("invalid_params", MSG_AGGREGATES)
        return list(dict.fromkeys(value))

    @staticmethod
    def _group_order_param(value, keys):
        """Return an order on groupby or aggregate items, or None."""
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_STRING:
            raise ApiError("invalid_params", MSG_GROUP_ORDER)
        items = []
        for item in value.split(","):
            match = GROUP_ORDER_ITEM_RE.fullmatch(item.strip())
            if not match or match.group(1) not in keys:
                raise ApiError("invalid_params", MSG_GROUP_ORDER)
            items.append(f"{match.group(1)} {(match.group(2) or 'asc').lower()}")
        return ", ".join(items)

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
        if (query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH)) or (
            type(limit) is not int or not 1 <= limit <= MAX_MODELS
        ):
            raise ApiError("invalid_params")
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
            "fields": result_fields,
        }
        return result, model._name, len(result_fields)

    def _op_search(self, params):
        model = self._model(params.get("model"))
        domain = self._domain_param(model, params.get("domain"))
        names = self._fields_param(model, params.get("fields"))
        order = self._order_param(model, params.get("order"))
        limit = self._int_param(params.get("limit"), DEFAULT_LIMIT, 1, MAX_SEARCH)
        offset = self._int_param(params.get("offset"), 0, 0, MAX_OFFSET)
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
            raise ApiError("invalid_params", MSG_IDS)
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
        limit = self._int_param(params.get("limit"), DEFAULT_LIMIT, 1, MAX_GROUPS)
        # Every group comes from the records the domain finds for the user;
        # values are formatted as Odoo 20's formatted_read_group does.
        rows = model._read_group(domain, groupby, aggregates, order=order, limit=limit)
        formatters = [self._groupby_formatter(model, spec) for spec in groupby]
        result = []
        for row in rows:
            group = {
                spec: formatter(value)
                for spec, formatter, value in zip(groupby, formatters, row, strict=False)
            }
            group.update(zip(aggregates, row[len(groupby):], strict=True))
            result.append(group)
        return {"groups": result}, model._name, len(result)

    @staticmethod
    def _groupby_formatter(model, spec):
        """Return the formatter of one groupby value (Odoo 20 web format)."""
        name, _colon, granularity = spec.partition(":")
        field = model._fields[name]
        if field.type in ("many2one", "many2many"):
            return lambda value: (value.id, value.sudo().display_name) if value else False
        if field.type in ("date", "datetime") and granularity in READ_GROUP_TIME_GRANULARITY:
            locale = get_lang(model.env).code
            server_format = (
                DEFAULT_SERVER_DATETIME_FORMAT
                if field.type == "datetime"
                else DEFAULT_SERVER_DATE_FORMAT
            )
            display_format = READ_GROUP_DISPLAY_FORMAT[granularity]

            def format_period(value):
                if not value:
                    return value
                range_start = value
                if field.type == "datetime":
                    tzinfo = None
                    if model.env.context.get("tz") in TIMEZONES:
                        tzinfo = ZoneInfo(model.env.context["tz"])
                        range_start = range_start.replace(tzinfo=tzinfo).astimezone(
                            datetime.timezone.utc
                        )
                    label = babel.dates.format_datetime(
                        range_start, format=display_format, tzinfo=tzinfo, locale=locale
                    )
                else:
                    label = babel.dates.format_date(value, format=display_format, locale=locale)
                if granularity == "week":
                    year, week = date_utils.weeknumber(babel.Locale.parse(locale), value)
                    label = f"W{week} {year:04}"
                return (range_start.strftime(server_format), label)

            return format_period
        return lambda value: value
