import fnmatch
import logging
import re

import psycopg2
from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain

from .oauth import SCOPE_ODOO_READ, SCOPE_READ, find_by_secret, parse_scope, same_resource


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
MAX_READ = 100
MAX_OFFSET = 100_000
MAX_FIELDS = 200
MAX_DOMAIN_ITEMS = 200
MAX_DOMAIN_DEPTH = 4
MAX_PATH_HOPS = 4
MAX_VALUES = 1000
MAX_STRING = 1000
FIELD_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")
ORDER_ITEM_RE = re.compile(r"([a-z_][a-z0-9_]*)(?: +(asc|desc))?", re.IGNORECASE)

MSG_FIELDS = "A requested field is unknown, hidden or not readable."
MSG_DOMAIN = (
    "The domain is not valid, or uses a field that is unknown, hidden or not readable."
)
MSG_ORDER = "The order is not valid: use 'field' or 'field desc' items on sortable readable fields."
MSG_RANGE = "limit or offset is out of range."
MSG_IDS = "ids must be a list of 1 to 100 record ids."
MSG_RECORDS = "Some records do not exist or you may not read them."

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

    # Operation -> (method, scope). The sql operation is not offered yet.
    _OPS = {
        "models": ("_op_models", SCOPE_ODOO_READ),
        "describe": ("_op_describe", SCOPE_ODOO_READ),
        "search": ("_op_search", SCOPE_ODOO_READ),
        "read": ("_op_read", SCOPE_ODOO_READ),
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
        """Run ``op`` as the current user; return ``(result, model, rows)``.

        The operation runs in a savepoint switched to a read-only transaction
        with a statement timeout, and the savepoint is always rolled back:
        PostgreSQL itself refuses any write. Errors raised by the ORM or the
        database become fixed API errors; their text never leaves the server.
        """
        if not isinstance(params, dict):
            raise ApiError("invalid_params")
        method = getattr(self, self._OPS[op][0])
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
        except (psycopg2.Error, UserError, ValidationError, ValueError, TypeError, KeyError) as exc:
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
            if model.has_field_access(field, "read") and not self._field_denied(field)
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

    def _path_model(self, model, path, relational_end=False):
        """Check a field path such as ``partner_id.country_id.code``.

        Every hop must be a readable, non-hidden field; every hop but the last
        (and the last too with ``relational_end``) a relational field to a
        model the user may read, whose search applies that model's rules.
        Returns the model the path ends on.
        """
        names = path.split(".") if isinstance(path, str) else []
        if not 0 < len(names) <= MAX_PATH_HOPS or not all(
            FIELD_NAME_RE.fullmatch(name) for name in names
        ):
            raise ApiError("invalid_params", MSG_DOMAIN)
        current = model
        for index, name in enumerate(names):
            field = self._readable_fields(current).get(name)
            if field is None:
                raise ApiError("invalid_params", MSG_DOMAIN)
            if index == len(names) - 1 and not relational_end:
                break
            if (
                not field.relational
                or field.bypass_search_access
                or not self._exposed_model(field.comodel_name)
                or not current.env[field.comodel_name].has_access("read")
            ):
                raise ApiError("invalid_params", MSG_DOMAIN)
            current = current.env[field.comodel_name]
        return current

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

    def _domain_param(self, model, value, depth=0):
        """Return ``value`` as a domain after checking every condition.

        The structure is parsed by Odoo's own domain machinery; each field
        path, including those inside ``any`` / ``not any`` sub-domains, is
        checked as the user.
        """
        if value is None:
            value = []
        if (
            not isinstance(value, list)
            or len(value) > MAX_DOMAIN_ITEMS
            or depth > MAX_DOMAIN_DEPTH
        ):
            raise ApiError("invalid_params", MSG_DOMAIN)
        for item in value:
            if item in ("&", "|", "!"):
                continue
            if not isinstance(item, list) or len(item) != 3:
                raise ApiError("invalid_params", MSG_DOMAIN)
            path, operator, operand = item
            if not isinstance(operator, str):
                raise ApiError("invalid_params", MSG_DOMAIN)
            if operator in ("any", "not any"):
                comodel = self._path_model(model, path, relational_end=True)
                self._domain_param(comodel, operand, depth + 1)
            else:
                self._path_model(model, path)
                self._check_operand(operand)
        try:
            return Domain(value)
        except (ValueError, TypeError) as exc:
            raise ApiError("invalid_params", MSG_DOMAIN) from exc

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
        limit = self._int_param(params.get("limit"), MAX_SEARCH, 1, MAX_SEARCH)
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
        records = model.browse(list(dict.fromkeys(ids)))
        # Missing and forbidden records are refused alike, so the answer does
        # not tell whether a hidden record exists.
        if len(records.exists()._filtered_access("read")) != len(records):
            raise ApiError("access_denied", MSG_RECORDS)
        rows = records.read(names)
        return {"records": rows}, model._name, len(rows)
