"""Permission-scoped SQL: the data API's ``sql`` operation.

The user's query never runs in Odoo's database. For every table it names,
the ORM builds, as the user, the SQL of ``Model._search([])`` (access
permissions, restrictions, ``_inherits`` parents, ``active_test=False``),
projected on the stored fields the user may read and the query mentions.
Odoo's own connection streams those rows out with ``COPY … TO STDOUT``; a
separate connection, logged in to an empty database on a separate PostgreSQL
instance (the sandbox) as a role without any privilege, streams them into
``ON COMMIT DROP`` temporary tables, then runs the user's query in a
read-only transaction under a time limit. The connection is rolled back and
closed afterwards. Whatever the query does, it only sees rows the user may
read: there is no other data in that session and no privilege to reach any.

Each concurrent query uses its own sandbox role (``<user>_1`` … ``<user>_N``),
so queries of different users cannot see or signal each other's sessions.
"""

import datetime
import decimal
import logging
import math
import os
import queue
import threading
import time
import uuid

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2 import sql as pgsql

from odoo import api, models
from odoo.tools import SQL, config, ormcache

from .api import ApiError
from .denylist import SQL_DENIED_MODELS, SQL_REVIEWED_READ_OVERRIDES, model_denied
from .oauth import get_config, get_config_int


try:
    from pglast import ast as pg_ast
    from pglast import parse_sql
    from pglast.parser import ParseError
except ImportError:  # declared in the manifest's external dependencies
    pg_ast = parse_sql = ParseError = None


_logger = logging.getLogger(__name__)

PASSWORD_ENV = "O3P_SQL_PASSWORD"
PASSWORD_CONFIG_KEY = "o3p_sql_password"
DEFAULT_SQL_PORT = 5432
DEFAULT_SQL_DATABASE = "o3p_sqlbox"
DEFAULT_SQL_USER = "o3p_sqlbox"
DEFAULT_SQL_SSLMODE = "prefer"
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_SQL_ROLES = 4
DEFAULT_SQL_USER_CONCURRENCY = 2
DEFAULT_SQL_MAX_ROWS = 200_000
DEFAULT_SQL_MAX_COPY_BYTES = 64_000_000
DEFAULT_SQL_LIMIT = 100
MAX_SQL_LIMIT = 1000
MAX_SQL_LENGTH = 20_000
MAX_ERROR_MESSAGE = 500
CONNECT_TIMEOUT = 5
# Seconds the watchdog waits beyond the statement timeout before cancelling.
WATCHDOG_GRACE = 1.0
# Advisory lock namespaces in Odoo's database (two-key form, int4 each).
LOCK_SANDBOX_ROLE = 55_623_681
LOCK_USER_SLOT = 55_623_682
MAX_USER_SLOTS = 64
# Methods that change what a read returns after the search: a model whose
# class overrides one of them is not exposed to SQL.
READ_OVERRIDES = ("read", "_read", "fetch", "_fetch_query", "search_fetch", "_read_format")
# Modules defining the ORM's own model classes (Odoo 18: odoo.models).
ORM_MODULES = ("odoo.models", "odoo.orm.")

MSG_ONE_SELECT = "Only one SELECT statement is allowed (no INTO, FOR UPDATE or FOR SHARE)."
MSG_TOO_MANY_ROWS = (
    "Table %(table)s holds more rows than SQL may copy (%(max)s); "
    "use the search or group operations for it."
)
MSG_TOO_MANY_BYTES = "The tables the query reads are too large; name fewer columns or tables."
MSG_USER_BUSY = "You already have the maximum number of SQL queries running."
MSG_SANDBOX_BUSY = "All SQL sandbox connections are busy; try again shortly."
MSG_NOT_CONFIGURED = "SQL queries are not configured on this server."
MSG_SANDBOX_DOWN = "The SQL sandbox could not be reached."


def sql_password():
    """The sandbox password: environment first, then odoo.conf; never stored."""
    return os.environ.get(PASSWORD_ENV) or config.get(PASSWORD_CONFIG_KEY) or ""


def gate_error(sqlstate, message, position=None):
    return ApiError(
        "query_error",
        details={"sqlstate": sqlstate, "position": position, "message": message[:MAX_ERROR_MESSAGE]},
    )


def _walk(node):
    """Yield every pglast node below ``node`` (iterative, pre-order)."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, pg_ast.Node):
            yield item
            stack.extend(getattr(item, attr) for attr in item)


def parse_query(query):
    """Check ``query`` and return ``(tables, columns)`` it may read.

    Exactly one ``SELECT`` (or ``VALUES``/``TABLE``) without ``INTO`` or a
    locking clause is accepted. ``tables`` are the relation names it
    mentions; ``columns`` the column names it mentions, or None when it
    asks for whole rows (``*`` or a table used as a value). Both are
    over-approximations: a name copied for nothing costs time, a name
    missed only makes the query fail; nothing here decides what is visible.
    """
    if parse_sql is None:
        raise ApiError("unavailable", MSG_NOT_CONFIGURED)
    try:
        statements = parse_sql(query)
    except ParseError as exc:
        location = exc.args[1] + 1 if len(exc.args) > 1 and isinstance(exc.args[1], int) else None
        raise gate_error("42601", str(exc.args[0]), location) from None
    if len(statements) != 1 or not isinstance(statements[0].stmt, pg_ast.SelectStmt):
        raise gate_error("0A000", MSG_ONE_SELECT)
    tables, aliases, columns, single_names, whole_rows = set(), set(), set(), set(), False
    for node in _walk(statements[0].stmt):
        if isinstance(node, pg_ast.SelectStmt) and (node.intoClause or node.lockingClause):
            raise gate_error("0A000", MSG_ONE_SELECT)
        if isinstance(node, pg_ast.RangeVar):
            tables.add(node.relname)
            aliases.add(node.relname)
            if node.alias:
                aliases.add(node.alias.aliasname)
        elif isinstance(node, pg_ast.Alias):
            aliases.add(node.aliasname)
        elif isinstance(node, pg_ast.ColumnRef):
            last = node.fields[-1]
            if isinstance(last, pg_ast.A_Star):
                whole_rows = True
            elif isinstance(last, pg_ast.String):
                columns.add(last.sval)
                if len(node.fields) == 1:
                    single_names.add(last.sval)
    # A bare table name or alias used as a value is a whole-row reference.
    if whole_rows or single_names & aliases:
        return tables, None
    return tables, columns


def json_value(value):
    """Return ``value`` (as psycopg2 decodes it) in a JSON-friendly form."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        if value == value.to_integral_value() and abs(value) < 2**53:
            return int(value)
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


class CopyTooLarge(Exception):
    pass


class CopyAborted(Exception):
    pass


_END = object()
_ABORT = object()


class CopyPipe:
    """Carries COPY text from Odoo's connection to the sandbox, chunk by chunk.

    Odoo's side writes (``copy_expert(… TO STDOUT, pipe)``) in the request
    thread; the sandbox side reads (``copy_expert(… FROM STDIN, pipe)``) in a
    helper thread. At most a few chunks are in memory at once.
    """

    def __init__(self, max_bytes):
        self.chunks = queue.Queue(maxsize=8)
        self.max_bytes = max_bytes
        self.bytes = 0
        self.rows = 0
        self.consumer_failed = threading.Event()
        self.consumer_error = None

    # Odoo's side
    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.bytes += len(data)
        # COPY text escapes newlines inside values: one newline, one row.
        self.rows += data.count(b"\n")
        if self.bytes > self.max_bytes:
            raise CopyTooLarge
        self._put(data)
        return len(data)

    def _put(self, item):
        while True:
            if self.consumer_failed.is_set():
                raise CopyAborted
            try:
                self.chunks.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def finish(self):
        self._put(_END)

    def abort(self):
        try:
            self._put(_ABORT)
        except CopyAborted:
            pass

    # Sandbox side
    def read(self, size=-1):
        item = self.chunks.get()
        if item is _END:
            return b""
        if item is _ABORT:
            raise CopyAborted
        return item

    readline = read

    def consume(self, cursor, statement):
        try:
            cursor.copy_expert(statement, self)
        except Exception as exc:  # noqa: BLE001 - handed over to the request thread
            self.consumer_error = exc
            self.consumer_failed.set()


class O3pMcpApiSql(models.AbstractModel):
    _inherit = "o3p.mcp.api"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.model
    def _sql_settings(self):
        def positive(key, default):
            value = get_config_int(self.env, key, default)
            return value if value > 0 else default

        sslmode = get_config(self.env, "o3p_mcp.sql_sslmode", DEFAULT_SQL_SSLMODE)
        return {
            "host": get_config(self.env, "o3p_mcp.sql_host").strip(),
            "port": positive("o3p_mcp.sql_port", DEFAULT_SQL_PORT),
            "database": get_config(self.env, "o3p_mcp.sql_database").strip() or DEFAULT_SQL_DATABASE,
            "user": get_config(self.env, "o3p_mcp.sql_user").strip() or DEFAULT_SQL_USER,
            "sslmode": sslmode if sslmode in SSL_MODES else DEFAULT_SQL_SSLMODE,
            "roles": positive("o3p_mcp.sql_roles", DEFAULT_SQL_ROLES),
            "user_concurrency": min(
                positive("o3p_mcp.sql_user_concurrency", DEFAULT_SQL_USER_CONCURRENCY), MAX_USER_SLOTS
            ),
            "max_rows": positive("o3p_mcp.sql_max_rows", DEFAULT_SQL_MAX_ROWS),
            "max_copy_bytes": positive("o3p_mcp.sql_max_copy_bytes", DEFAULT_SQL_MAX_COPY_BYTES),
        }

    @api.model
    def _sql_connect(self, settings, slot, timeout_ms, **overrides):
        """Open a sandbox connection as role ``<user>_<slot>``."""
        params = {
            "host": settings["host"],
            "port": settings["port"],
            "dbname": settings["database"],
            "user": f"{settings['user']}_{slot}",
            "password": sql_password(),
            "sslmode": settings["sslmode"],
            "connect_timeout": CONNECT_TIMEOUT,
            "application_name": "o3p_mcp",
            "options": f"-c statement_timeout={int(timeout_ms)} "
            f"-c idle_in_transaction_session_timeout={int(timeout_ms) + 5000}",
            **overrides,
        }
        return psycopg2.connect(**params)

    # ------------------------------------------------------------------
    # What SQL may expose
    # ------------------------------------------------------------------

    @api.model
    @ormcache()
    def _sql_table_index(self):
        """Map each table to the model storing it or the many2many fields using it.

        ``{table: ("model", (model,…)) | ("m2m", ((model, field),…))}``, from
        the registry alone; denylists and rights are applied per query.
        """
        index = {}
        for name in self.env.registry:
            model = self.env[name]
            if model._abstract or model._transient or not model._table:
                continue
            kind, names = index.setdefault(model._table, ("model", []))
            if kind == "model":
                names.append(name)
            for field in model._fields.values():
                if field.type == "many2many" and field.store and field.relation:
                    kind, entries = index.setdefault(field.relation, ("m2m", []))
                    if kind == "m2m":
                        entries.append((name, field.name))
        return {table: (kind, tuple(items)) for table, (kind, items) in index.items()}

    @api.model
    @ormcache("name")
    def _sql_model_overrides_read(self, name):
        """Tell whether a module overrides how model ``name`` reads records."""
        if name in SQL_REVIEWED_READ_OVERRIDES:
            return False
        cls = type(self.env[name])
        for method in READ_OVERRIDES:
            owner = next((klass for klass in cls.__mro__ if method in vars(klass)), None)
            if owner is not None and not owner.__module__.startswith(ORM_MODULES):
                return True
        return False

    def _sql_model_denied(self, name):
        """Models SQL never exposes: the data API's hidden models (the shared
        denylist), denylist.SQL_DENIED_MODELS, models whose reads an addon
        customises, and models stored partly in such a model."""
        if not self._exposed_model(name):
            return True
        if model_denied(name, SQL_DENIED_MODELS):
            return True
        if self._sql_model_overrides_read(name):
            return True
        # A model stored partly in a denied parent is denied with it.
        return any(self._sql_model_denied(parent) for parent in self.env[name]._inherits)

    def _sql_model(self, name):
        """Return model ``name`` for the user if SQL may expose it, else None."""
        if self._sql_model_denied(name):
            return None
        model = self.env[name].with_context(active_test=False)
        return model if model.has_access("read") else None

    def _sql_columns(self, model, wanted):
        fields = [
            field
            for name, field in self._readable_fields(model).items()
            if field.store
            and field.column_type
            and not field.inherited
            and (wanted is None or name in wanted)
        ]
        return fields or [model._fields["id"]]

    def _sql_sources(self, tables, columns):
        """Return ``[(table, model name or None, select SQL)]`` the user may read.

        A table is left out (and the query then fails with "relation does not
        exist", as for a table that does not exist) when it is unknown,
        shared by several models, hidden, or not readable by the user.
        """
        index = self._sql_table_index()
        sources = []
        for table in sorted(tables):
            kind, items = index.get(table, (None, ()))
            if kind == "model" and len(items) == 1:
                model = self._sql_model(items[0])
                if model is None:
                    continue
                fields = self._sql_columns(model, columns)
                query = model._search([])
                # Odoo 18: _field_to_sql renders translated fields in the
                # user's language and company-dependent ones for the user's
                # company, and refuses fields outside the user's groups.
                select = query.select(
                    *[
                        SQL(
                            "%s AS %s",
                            model._field_to_sql(query.table, field.name, query),
                            SQL.identifier(field.name),
                        )
                        for field in fields
                    ]
                )
                sources.append((table, model._name, select))
            elif kind == "m2m":
                select = self._sql_relation_select(table, items)
                if select is not None:
                    sources.append((table, None, select))
        return sources

    def _sql_relation_select(self, table, entries):
        """Rows of a many2many table whose both sides the user may read.

        Every field stored in the table must be readable by the user and
        point between models SQL exposes; otherwise the table is left out.
        """
        selected = None
        for model_name, field_name in entries:
            model = self._sql_model(model_name)
            if model is None:
                return None
            field = self._readable_fields(model).get(field_name)
            if field is None:
                return None
            comodel = self._sql_model(field.comodel_name)
            if comodel is None:
                return None
            if selected is None:
                selected = (model, field, comodel)
        model, field, comodel = selected
        column1, column2 = SQL.identifier(field.column1), SQL.identifier(field.column2)
        return SQL(
            "SELECT %s, %s FROM %s WHERE %s IN %s AND %s IN %s",
            column1,
            column2,
            SQL.identifier(table),
            column1,
            model._search([]).subselect(),
            column2,
            comodel._search([]).subselect(),
        )

    # ------------------------------------------------------------------
    # Concurrency
    # ------------------------------------------------------------------

    def _sql_try_lock(self, namespace, key):
        self.env.cr.execute(
            SQL("SELECT pg_try_advisory_xact_lock(%s, %s)", namespace, key % 2**31)
        )
        return self.env.cr.fetchone()[0]

    def _sql_acquire_slots(self, settings):
        """Reserve one of the user's slots and a free sandbox role.

        Transaction-level advisory locks in Odoo's database: they are released
        when the request's transaction ends, even if the worker dies.
        """
        base = (self.env.uid % 2**24) * MAX_USER_SLOTS
        if not any(
            self._sql_try_lock(LOCK_USER_SLOT, base + slot)
            for slot in range(settings["user_concurrency"])
        ):
            raise ApiError("too_many_requests", MSG_USER_BUSY)
        for role in range(1, settings["roles"] + 1):
            if self._sql_try_lock(LOCK_SANDBOX_ROLE, role):
                return role
        raise ApiError("too_many_requests", MSG_SANDBOX_BUSY)

    # ------------------------------------------------------------------
    # The operation
    # ------------------------------------------------------------------

    def _op_sql(self, params):
        started = time.monotonic()
        query = params.get("query")
        limit = params.get("limit", DEFAULT_SQL_LIMIT)
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_SQL_LENGTH
            or type(limit) is not int
            or not 1 <= limit <= MAX_SQL_LIMIT
        ):
            raise ApiError("invalid_params")
        settings = self._sql_settings()
        if not settings["host"] or not sql_password() or parse_sql is None:
            raise ApiError("unavailable", MSG_NOT_CONFIGURED)
        tables, columns = parse_query(query)
        sources = self._sql_sources(tables, columns)
        names = [table for table, _model, _select in sources]
        role = self._sql_acquire_slots(settings)
        timeout_ms = self._timeout_seconds() * 1000
        deadline = started + timeout_ms / 1000
        try:
            connection = self._sql_connect(settings, role, timeout_ms)
        except psycopg2.Error as exc:
            _logger.warning("MCP SQL: the sandbox could not be reached (%s)", type(exc).__name__)
            raise ApiError("unavailable", MSG_SANDBOX_DOWN, tables=names) from None
        watchdog = None
        try:
            cursor = connection.cursor()
            copied = self._sql_copy(cursor, sources, settings, names)
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise ApiError("timeout", tables=names)
            cursor.execute("SET LOCAL transaction_read_only = on")
            self._sql_limit_query(cursor, remaining_ms)
            # statement_timeout is set by the session and could be raised by a
            # crafted query; the watchdog cancels from outside regardless.
            watchdog = threading.Timer(remaining_ms / 1000 + WATCHDOG_GRACE, connection.cancel)
            watchdog.start()
            try:
                cursor.execute(query)
                description = cursor.description or ()
                rows = cursor.fetchmany(limit + 1) if description else []
            except psycopg2.Error as exc:
                raise self._sql_query_error(exc, names) from None
            finally:
                watchdog.cancel()
            columns_out = self._sql_column_types(cursor, description)
        finally:
            if watchdog:
                watchdog.cancel()
            try:
                connection.rollback()
            finally:
                connection.close()
        truncated = len(rows) > limit
        rows = [[json_value(value) for value in row] for row in rows[:limit]]
        result = {
            "columns": columns_out,
            "rows": rows,
            "truncated": truncated,
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "tables": [
                {"name": table, "model": model, "rows": copied[table]}
                for table, model, _select in sources
            ],
        }
        return result, None, len(rows), names

    @staticmethod
    def _sql_limit_query(cursor, milliseconds):
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f"{milliseconds}ms"])

    def _sql_copy(self, cursor, sources, settings, names):
        """Create and fill one temporary table per source; return row counts."""
        odoo_cursor = self.env.cr
        budget = settings["max_copy_bytes"]
        copied = {}
        for table, _model, select in sources:
            odoo_cursor.execute(SQL("SELECT * FROM (%s) AS q LIMIT 0", select))
            description = [(column.name, column.type_code) for column in odoo_cursor.description]
            odoo_cursor.execute(
                SQL(
                    "SELECT oid, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)",
                    [oid for _name, oid in description],
                )
            )
            types = dict(odoo_cursor.fetchall())
            identifier = pgsql.Identifier(table)
            cursor.execute(
                pgsql.SQL("CREATE TEMPORARY TABLE {} ({}) ON COMMIT DROP").format(
                    identifier,
                    pgsql.SQL(", ").join(
                        pgsql.SQL("{} {}").format(pgsql.Identifier(name), pgsql.SQL(types[oid]))
                        for name, oid in description
                    ),
                )
            )
            pipe = CopyPipe(budget)
            consumer = threading.Thread(
                target=pipe.consume,
                args=(cursor, pgsql.SQL("COPY pg_temp.{} FROM STDIN").format(identifier).as_string(cursor)),
                name="o3p_mcp_sql_copy",
                daemon=True,
            )
            consumer.start()
            try:
                statement = odoo_cursor.mogrify(
                    SQL("COPY (SELECT * FROM (%s) AS q LIMIT %s) TO STDOUT", select, settings["max_rows"] + 1)
                ).decode()
                odoo_cursor._obj.copy_expert(statement, pipe)
                pipe.finish()
            except CopyTooLarge:
                pipe.abort()
                consumer.join()
                raise ApiError("result_too_large", MSG_TOO_MANY_BYTES, tables=names) from None
            except CopyAborted:
                consumer.join()
                raise self._sql_copy_error(pipe.consumer_error, names) from None
            except Exception:
                pipe.abort()
                consumer.join()
                raise
            consumer.join()
            if pipe.consumer_error is not None:
                raise self._sql_copy_error(pipe.consumer_error, names)
            if pipe.rows > settings["max_rows"]:
                raise ApiError(
                    "result_too_large",
                    MSG_TOO_MANY_ROWS % {"table": table, "max": settings["max_rows"]},
                    tables=names,
                )
            budget -= pipe.bytes
            copied[table] = pipe.rows
            cursor.execute(pgsql.SQL("ANALYZE pg_temp.{}").format(identifier))
        return copied

    @staticmethod
    def _sql_copy_error(error, names):
        if isinstance(error, pg_errors.QueryCanceled):
            return ApiError("timeout", tables=names)
        _logger.warning("MCP SQL: copying into the sandbox failed (%s)", type(error).__name__)
        return ApiError("query_error", tables=names)

    @staticmethod
    def _sql_query_error(error, names):
        """Map a sandbox error about the user's query to an API error.

        The sandbox holds only rows the user may read, so PostgreSQL's own
        message cannot reveal anything else; it is passed on, capped.
        """
        if isinstance(error, pg_errors.QueryCanceled):
            return ApiError("timeout", tables=names)
        diag = getattr(error, "diag", None)
        position = getattr(diag, "statement_position", None)
        message = getattr(diag, "message_primary", None) or str(error).splitlines()[0]
        return ApiError(
            "query_error",
            details={
                "sqlstate": getattr(error, "pgcode", None),
                "position": int(position) if position and position.isdigit() else None,
                "message": message[:MAX_ERROR_MESSAGE],
            },
            tables=names,
        )

    @staticmethod
    def _sql_column_types(cursor, description):
        if not description:
            return []
        oids = sorted({column.type_code for column in description})
        cursor.execute("SELECT oid, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)", [oids])
        types = dict(cursor.fetchall())
        return [{"name": column.name, "type": types.get(column.type_code, "unknown")} for column in description]
