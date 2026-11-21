import fnmatch
import logging
import math
from datetime import datetime

import psycopg2
from psycopg2 import errors as pg_errors

from odoo import Command, api, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY, _as_validation_error

from .api import MAX_FIELDS, ApiError, invalid
from .denylist import patterns


_logger = logging.getLogger(__name__)

MAX_CREATE = 20
MAX_UPDATE = 100
MAX_TEXT = 100_000
MAX_LINKS = 1000
MAX_MESSAGE = 500
MSG_WRITE = "You are not allowed to change this."
MSG_REFUSED = "Odoo refused the change."
MSG_VALUES = "The values are not valid for these fields."
MSG_RECORDS = "records must be a list of 1 to 20 objects of field values."
# Models the data API never writes, whatever the list of writable models
# says: their records grant access, hold code Odoo runs, or send messages
# without the checks of a record's own form.
WRITE_NEVER = (
    "ir.*",
    "res.users*",
    "res.groups*",
    "base.automation*",
    "o3p.mcp.*",
    "mail.mail",
    "mail.message",
    "mail.followers",
    "mail.notification",
    "mail.template*",
    "mail.alias*",
    "sms.*",
    "payment.*",
)
# Columns Odoo maintains itself.
MAGIC_FIELDS = frozenset({*models.MAGIC_COLUMNS, "display_name"})
TEXT_TYPES = frozenset({"char", "text", "html"})
NUMBER_TYPES = frozenset({"float", "monetary"})
WRITE_TYPES = frozenset(
    {*TEXT_TYPES, *NUMBER_TYPES, "integer", "boolean", "date", "datetime", "selection", "many2one", "many2many"}
)


def refused(message):
    """Odoo's own answer to a write, as it shows it to the user."""
    return ApiError(
        "query_error", MSG_REFUSED, details={"message": str(message)[:MAX_MESSAGE]}
    )


class O3pMcpApiWrite(models.AbstractModel):
    _inherit = "o3p.mcp.api"

    # ------------------------------------------------------------------
    # What may be written
    # ------------------------------------------------------------------

    @api.model
    def _writable_model_patterns(self):
        """The models an administrator opened for writing; none by default."""
        return patterns(self._setting("o3p_mcp.api_writable_models", ""), ())

    def _model_writable(self, name):
        """Tell whether records of model ``name`` may be written at all."""
        return (
            any(fnmatch.fnmatchcase(name, item) for item in self._writable_model_patterns())
            and not any(fnmatch.fnmatchcase(name, item) for item in WRITE_NEVER)
            and not self._model_hides_on_read(name)
        )

    def _model_can_be_opened(self, name):
        """Tell whether records of model ``name`` could be written once an
        administrator lists it: the model is exposed and is not one the API
        never writes."""
        return (
            self._exposed_model(name)
            and not any(fnmatch.fnmatchcase(name, item) for item in WRITE_NEVER)
            and not self._model_hides_on_read(name)
        )

    def _write_model(self, name, operation):
        """Return model ``name`` for the user's ``create`` or ``write``, or raise."""
        model = self._model(name)
        if not self._model_writable(model._name):
            raise invalid("model_not_writable", model=model._name, code="access_denied", message=MSG_WRITE)
        if not model.has_access(operation):
            raise invalid("no_write_access", model=model._name, code="access_denied", message=MSG_WRITE)
        return model

    def _field_takes_values(self, field):
        """Tell whether the API sets ``field`` at all, whoever the user is.

        Odoo's ORM accepts values for fields declared read-only; the API does
        not. A related field is refused because it changes another record.
        """
        return bool(
            field.name not in MAGIC_FIELDS
            and field.type in WRITE_TYPES
            and not field.readonly
            and not field.related
            and (field.store or field.inverse)
            and (field.type != "html" or (field.sanitize and not field.sanitize_overridable))
            and (not field.relational or self._exposed_model(field.comodel_name))
        )

    def _field_writable(self, model, field):
        """Tell whether the user may set ``field``, a readable field of ``model``."""
        return self._field_takes_values(field) and field.is_accessible(model.env)

    def _writable_fields(self, model):
        return {
            name: field
            for name, field in self._readable_fields(model).items()
            if self._field_writable(model, field)
        }

    def _in_company(self, model, company):
        """Return ``model`` with ``company`` as the user's active company."""
        if company is None:
            return model
        allowed = self.env.user.company_ids.ids
        if type(company) is not int or company not in allowed:
            raise invalid("bad_company", "company")
        others = [company_id for company_id in allowed if company_id != company]
        return model.with_context(allowed_company_ids=[company, *others])

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    def _related_ids(self, model, field, value, path):
        """Return ``value``, record ids of ``field``'s model the user may read."""
        if (
            not isinstance(value, list)
            or len(value) > MAX_LINKS
            or not all(type(item) is int and item > 0 for item in value)
        ):
            raise invalid("bad_value", path, message=MSG_VALUES)
        ids = list(dict.fromkeys(value))
        comodel = model.env[field.comodel_name]
        if not comodel.has_access("read"):
            raise invalid("no_relation_access", path, code="access_denied", message=MSG_WRITE)
        found = comodel.with_context(active_test=False).search_count([("id", "in", ids)])
        if found != len(ids):
            raise invalid("unknown_record", path, message=MSG_VALUES)
        return ids

    def _write_value(self, model, field, value):
        """Return ``value`` as the ORM takes it for ``field``, or raise."""
        name, kind = field.name, field.type
        bad = invalid("bad_value", name, message=MSG_VALUES)
        if kind == "boolean":
            if type(value) is not bool:
                raise bad
            return value
        if kind == "many2many":
            if isinstance(value, dict):
                if not value or set(value) - {"add", "remove"}:
                    raise bad
                return [
                    *(Command.link(i) for i in self._related_ids(model, field, value.get("add", []), name)),
                    *(Command.unlink(i) for i in self._related_ids(model, field, value.get("remove", []), name)),
                ]
            return [Command.set(self._related_ids(model, field, value, name))]
        # Every other type is emptied with false.
        if value is False or value is None:
            return False
        if kind in TEXT_TYPES:
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise bad
            return value
        if kind == "integer":
            if type(value) is not int:
                raise bad
            return value
        if kind in NUMBER_TYPES:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise bad
            return value
        if kind in ("date", "datetime"):
            pattern = "%Y-%m-%d" if kind == "date" else "%Y-%m-%d %H:%M:%S"
            try:
                if not isinstance(value, str):
                    raise ValueError
                datetime.strptime(value, pattern)
            except ValueError:
                raise bad from None
            return value
        if kind == "selection":
            if not isinstance(value, str) or value not in field.get_values(model.env):
                raise bad
            return value
        if kind == "many2one":
            if type(value) is not int:
                raise bad
            return self._related_ids(model, field, [value], name)[0]
        raise invalid("not_writable", name, message=MSG_VALUES)

    def _write_values(self, model, values, writable=None):
        """Return ``values`` converted for the ORM; every field writable.

        ``writable`` narrows the fields that take a value, by name.
        """
        if not isinstance(values, dict) or not 0 < len(values) <= MAX_FIELDS:
            raise invalid("bad_value", "values", message=MSG_VALUES)
        if writable is None:
            writable = self._writable_fields(model)
        result = {}
        for name, value in values.items():
            if not isinstance(name, str) or name not in model._fields:
                raise invalid("unknown_field", name if isinstance(name, str) else None, message=MSG_VALUES)
            if name not in writable:
                raise invalid("not_writable", name, message=MSG_VALUES)
            result[name] = self._write_value(model, writable[name], value)
        return result

    def _check_required(self, model, vals_list):
        """Name a required field a new record has no value for."""
        required = [
            name
            for name, field in model._fields.items()
            if field.required and field.store and not field.compute and name not in MAGIC_FIELDS
        ]
        defaults = model.default_get(required)
        for vals in vals_list:
            for name in required:
                given = vals.get(name, defaults.get(name))
                if given is None or given is False or given == "":
                    # A boolean or a number has a value in any case.
                    if model._fields[name].type in ("boolean", "integer", *NUMBER_TYPES):
                        continue
                    raise invalid("required_missing", name, message=MSG_VALUES)

    def _records_to_write(self, model, ids):
        """Return the records ``ids`` of ``model``, all readable and writable.

        An id that does not exist and one the user may not read get the same
        answer, so the answer does not tell whether a hidden record exists.
        """
        if (
            not isinstance(ids, list)
            or not 0 < len(ids) <= MAX_UPDATE
            or not all(type(item) is int and item > 0 for item in ids)
        ):
            raise invalid("bad_value", "ids")
        ids = list(dict.fromkeys(ids))
        if model.with_context(active_test=False).search_count([("id", "in", ids)]) != len(ids):
            raise invalid("unknown_record", "ids")
        records = model.browse(ids)
        records.check_access("write")
        return records

    # ------------------------------------------------------------------
    # Running a write
    # ------------------------------------------------------------------

    def _run_write(self, method, params):
        """Run a writing operation as the current user.

        It runs in a savepoint with the statement timeout and is flushed
        inside it, so constraints and computed fields have run before the
        savepoint is released; any error rolls it back, with the messages and
        notifications it queued for after the commit. A conflict with another
        transaction is raised as it is, so that Odoo runs the request again.
        """
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("SHOW statement_timeout")
        previous = cr.fetchone()[0]
        queued = (list(cr.postcommit._funcs), dict(cr.postcommit.data))
        try:
            with cr.savepoint():
                cr.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{self._timeout_seconds() * 1000}ms"],
                )
                outcome = method(params)
                self.env.flush_all()
        except Exception as exc:  # noqa: BLE001 - every failure undoes the write
            cr.postcommit._funcs.clear()
            cr.postcommit._funcs.extend(queued[0])
            cr.postcommit.data.clear()
            cr.postcommit.data.update(queued[1])
            self.env.invalidate_all(flush=False)
            if isinstance(exc, PG_CONCURRENCY_EXCEPTIONS_TO_RETRY):
                raise
            raise self._write_error(exc) from exc
        finally:
            cr.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
        return outcome

    def _write_error(self, exc):
        """Return the API error for ``exc``, raised while writing."""
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, AccessError):
            return invalid("no_record_access", code="access_denied", message=MSG_WRITE)
        if isinstance(exc, pg_errors.QueryCanceled):
            return ApiError("timeout")
        if isinstance(exc, (UserError, ValidationError)):
            return refused(exc.args[0] if exc.args else "")
        if isinstance(exc, psycopg2.IntegrityError):
            # A database constraint: Odoo's message for it names the
            # constraint or the field, as the web client shows it.
            try:
                return refused(_as_validation_error(self.env, exc).args[0])
            except Exception:  # noqa: BLE001 - fall back to the fixed answer
                _logger.info("MCP data API: no message for %s", type(exc).__name__)
        _logger.warning("MCP data API write failed: %s", type(exc).__name__)
        return ApiError("query_error", MSG_REFUSED)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    @staticmethod
    def _written(records, names):
        result = {
            "records": [{"id": record.id, "display_name": record.display_name} for record in records]
        }
        log = {
            "record_ids": ",".join(str(record_id) for record_id in records.ids)[:1000],
            "field_names": ",".join(sorted(names))[:1000],
        }
        return result, records._name, len(records), log

    def _op_create(self, params):
        model = self._write_model(params.get("model"), "create")
        model = self._in_company(model, params.get("company")).with_context(o3p_mcp_write=True)
        rows = params.get("records")
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_CREATE:
            raise invalid("bad_value", "records", message=MSG_RECORDS)
        vals_list = [self._write_values(model, row) for row in rows]
        self._check_required(model, vals_list)
        # The names the caller gave: create() may add its own to the values.
        names = {name for vals in vals_list for name in vals}
        records = model.create(vals_list)
        return self._written(records, names)

    def _op_update(self, params):
        model = self._write_model(params.get("model"), "write")
        model = self._in_company(model, params.get("company")).with_context(o3p_mcp_write=True)
        records = self._records_to_write(model, params.get("ids"))
        vals = self._write_values(model, params.get("values"))
        names = set(vals)
        records.write(vals)
        return self._written(records, names)
