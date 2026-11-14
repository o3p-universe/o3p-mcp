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
import ipaddress
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
from odoo.tools import SQL, config

from .api import ApiError
from .denylist import READ_OVERRIDES_REVIEWED, SQL_DENIED_MODELS, model_denied, overrides_read


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
# The sandbox connection's libpq sslmode. "auto" (the default) means
# "prefer" for a sandbox on this host (see sandbox_is_local) and
# "verify-full" anywhere else, where TLS must not be strippable.
SSL_AUTO = "auto"
DEFAULT_SQL_SSLMODE = SSL_AUTO
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
# Rows are fetched from a cursor in the sandbox. Each row's values are
# measured there (bytes of their text form, plus this much per value for
# the decoded Python objects) and a row larger than the largest answer
# arrives with its values emptied, so the worker never receives more than
# FETCH_MEMORY bytes of values per fetch, nor more than the largest answer
# in all.
VALUE_OVERHEAD = 16
FETCH_MEMORY = 16_000_000
MAX_FETCH_ROWS = 100
CURSOR_NAME = "o3p_mcp_rows"
# Advisory lock namespaces in Odoo's database (two-key form, int4 each).
LOCK_SANDBOX_ROLE = 55_623_681
LOCK_USER_SLOT = 55_623_682
MAX_USER_SLOTS = 64

MSG_ONE_SELECT = "Only one SELECT statement is allowed (no INTO, FOR UPDATE or FOR SHARE)."
MSG_TOO_MANY_ROWS = (
    "Table %(table)s holds more rows than SQL may copy (%(max)s); "
    "use the search or group operations for it."
)
MSG_TOO_MANY_BYTES = "The tables the query reads are too large; name fewer columns or tables."
MSG_ANSWER_TOO_LARGE = (
    "The answer is too large; select fewer or smaller values, or ask for fewer rows."
)
MSG_USER_BUSY = "You already have the maximum number of SQL queries running."
MSG_SANDBOX_BUSY = "All SQL sandbox connections are busy; try again shortly."
MSG_NOT_CONFIGURED = "SQL queries are not configured on this server."
MSG_SANDBOX_DOWN = "The SQL sandbox could not be reached."


def sql_password():
    """The sandbox password: environment first, then odoo.conf; never stored."""
    return os.environ.get(PASSWORD_ENV) or config.get(PASSWORD_CONFIG_KEY) or ""


def sandbox_is_local(host):
    """Tell whether ``host`` names a sandbox on this host.

    Local: a unix socket directory, ``localhost``, a loopback address, or a
    single-label name such as a Docker or Compose service name (``sandbox``),
    which Docker resolves on the host's own bridge network. Anything else (a
    dotted name, another address) is a remote host.
    """
    host = (host or "").strip()
    if not host or host.startswith("/") or host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return "." not in host and ":" not in host


def effective_sslmode(host, configured):
    """The sslmode to use: the configured one, or the default for ``host``."""
    if configured in SSL_MODES:
        return configured
    return "prefer" if sandbox_is_local(host) else "verify-full"


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
    """Check ``query`` and return ``(tables, columns, statement, offset)``.

    Exactly one ``SELECT`` (or ``VALUES``/``TABLE``) without ``INTO`` or a
    locking clause is accepted. ``tables`` are the relation names it
    mentions; ``columns`` the column names it mentions, or None when it
    asks for whole rows (``*`` or a table used as a value). Both are
    over-approximations: a name copied for nothing costs time, a name
    missed only makes the query fail; nothing here decides what is visible.
    ``statement`` is the statement's own text (without the surrounding
    comments, spaces and semicolon) and ``offset`` where it starts in
    ``query``, in characters.
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
    offset, length = statements[0].stmt_location, statements[0].stmt_len
    statement = query[offset : offset + length] if length else query[offset:]
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
        return tables, None, statement, offset
    return tables, columns, statement, offset


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
        params = self.env["ir.config_parameter"].sudo()

        def positive(key, default):
            value = params.get_int(key, default)
            return value if value > 0 else default

        host = params.get_str("o3p_mcp.sql_host", "").strip()
        configured = params.get_str("o3p_mcp.sql_sslmode", DEFAULT_SQL_SSLMODE)
        return {
            "host": host,
            "port": positive("o3p_mcp.sql_port", DEFAULT_SQL_PORT),
            "database": params.get_str("o3p_mcp.sql_database", "").strip() or DEFAULT_SQL_DATABASE,
            "user": params.get_str("o3p_mcp.sql_user", "").strip() or DEFAULT_SQL_USER,
            "sslmode_setting": configured if configured in SSL_MODES else SSL_AUTO,
            "sslmode": effective_sslmode(host, configured),
            "sslrootcert": params.get_str("o3p_mcp.sql_sslrootcert", "").strip(),
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
            **({"sslrootcert": settings["sslrootcert"]} if settings.get("sslrootcert") else {}),
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
    @api.ormcache()
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
    @api.ormcache("name")
    def _sql_model_overrides_read(self, name):
        """Tell whether an addon changes what model ``name``'s reads return.

        The shared denylist module decides, as for the other operations:
        a read override hides values that a copy of the rows would show,
        unless the override is in its reviewed list.
        """
        return name not in READ_OVERRIDES_REVIEWED and overrides_read(type(self.env[name]))

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
                select = query.select(
                    *[SQL("%s AS %s", query.table[field.name], SQL.identifier(field.name)) for field in fields]
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
        tables, columns, statement, offset = parse_query(query)
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
                description, rows = self._sql_fetch(cursor, statement, offset, limit, names)
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

    def _sql_fetch(self, cursor, statement, offset, limit, names):
        """Run ``statement`` through a cursor; return its description and rows.

        At most ``limit + 1`` rows are fetched, in small batches. The
        statement is wrapped so that the sandbox reports each row's size and
        empties rows larger than the largest answer; an oversized row, or
        rows adding up to more than the largest answer, end the query with
        result_too_large before more data arrives.
        """
        cap = self._max_response_bytes()
        prefix = "SELECT * FROM (\n"
        self._sql_execute(cursor, prefix, statement, "\n) AS o3p_q LIMIT 0", offset, names)
        description = list(cursor.description)
        width = len(description)
        if width:
            columns = [f"o3p_c{index}" for index in range(width)]
            size = " + ".join(f"coalesce(octet_length(o3p_q.{c}::text)::bigint, 0)" for c in columns)
            values = ", ".join(f"CASE WHEN o3p_r.o3p_size <= {cap} THEN o3p_r.{c} END" for c in columns)
            prefix = (
                f"DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR SELECT {values}, o3p_r.o3p_size"
                f" FROM (SELECT o3p_q.*, {size} + {width * VALUE_OVERHEAD} AS o3p_size FROM (\n"
            )
            suffix = f"\n) AS o3p_q({', '.join(columns)})) AS o3p_r"
        else:
            prefix, suffix = f"DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR \n", "\n"
        self._sql_execute(cursor, prefix, statement, suffix, offset, names)
        batch = max(1, min(MAX_FETCH_ROWS, FETCH_MEMORY // cap))
        rows, total = [], 0
        while len(rows) <= limit:
            wanted = min(batch, limit + 1 - len(rows))
            try:
                cursor.execute(f"FETCH FORWARD {wanted} FROM {CURSOR_NAME}")
                chunk = cursor.fetchall()
            except psycopg2.Error as exc:
                raise self._sql_query_error(exc, names) from None
            for row in chunk:
                if len(rows) < limit:
                    row_size = row[-1] if width else 0
                    total += row_size
                    if row_size > cap or total > cap:
                        raise ApiError("result_too_large", MSG_ANSWER_TOO_LARGE, tables=names)
                rows.append(row[:-1] if width else row)
            if len(chunk) < wanted:
                break
        return description, rows

    def _sql_execute(self, cursor, prefix, statement, suffix, offset, names):
        """Execute the user's statement wrapped in our own SQL.

        PostgreSQL's error positions are moved back to the user's query.
        """
        try:
            cursor.execute(prefix + statement + suffix)
        except psycopg2.Error as exc:
            raise self._sql_query_error(exc, names, (len(prefix), len(statement), offset)) from None

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
    def _sql_query_error(error, names, placement=None):
        """Map a sandbox error about the user's query to an API error.

        The sandbox holds only rows the user may read, so PostgreSQL's own
        message cannot reveal anything else; it is passed on, capped.
        ``placement`` is ``(prefix length, statement length, offset)`` of the
        user's statement in the SQL that ran, to report positions in the
        user's query; a position outside the statement is not reported.
        """
        if isinstance(error, pg_errors.QueryCanceled):
            return ApiError("timeout", tables=names)
        diag = getattr(error, "diag", None)
        position = getattr(diag, "statement_position", None)
        position = int(position) if position and position.isdigit() else None
        if position is not None:
            shift, length, offset = placement or (0, None, 0)
            position -= shift
            if position < 1 or (length is not None and position > length):
                position = None
            else:
                position += offset
        message = getattr(diag, "message_primary", None) or str(error).splitlines()[0]
        return ApiError(
            "query_error",
            details={
                "sqlstate": getattr(error, "pgcode", None),
                "position": position,
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

    # ------------------------------------------------------------------
    # Settings check
    # ------------------------------------------------------------------

    @api.model
    def _sql_check(self):
        """Connect as every sandbox role and verify it can do nothing else.

        Returns ``(level, lines)``, level ``success``, ``warning`` (the sandbox
        is remote and its connection not verify-full) or ``danger``. Each
        role must log in to the sandbox database
        and nowhere else, hold no attribute, membership or privilege beyond
        CONNECT and TEMPORARY there, find no relation, extension or file
        function it could use, and fail to log in to Odoo's own database.
        The sandbox must not be the PostgreSQL instance holding Odoo's
        database.
        """
        _ = self.env._
        settings = self._sql_settings()
        if not settings["host"]:
            return "danger", [_("Set the sandbox host first.")]
        problems, notes = [], []
        warnings = self._sql_transport_warnings(settings)
        if parse_sql is None:
            problems.append(_("The Python package pglast is not installed."))
        if not sql_password():
            problems.append(
                _(
                    "No sandbox password: set the %(env)s environment variable or %(key)s in the Odoo configuration file.",
                    env=PASSWORD_ENV,
                    key=PASSWORD_CONFIG_KEY,
                )
            )
            return "danger", problems + warnings
        odoo_db = self.env.cr.dbname
        for slot in range(1, settings["roles"] + 1):
            role = f"{settings['user']}_{slot}"
            try:
                connection = self._sql_connect(settings, slot, 10_000)
            except psycopg2.Error as exc:
                problems.append(
                    _("%(role)s cannot log in to the sandbox: %(error)s", role=role, error=str(exc).strip().splitlines()[0][:200])
                )
                continue
            try:
                problems += [f"{role}: {problem}" for problem in self._sql_check_role(connection, odoo_db, notes)]
            finally:
                connection.rollback()
                connection.close()
            if self._sql_can_reach_odoo(role):
                problems.append(_("%(role)s can log in to the Odoo database %(db)s.", role=role, db=odoo_db))
        if problems:
            return "danger", problems + warnings
        lines = [
            _(
                "%(count)s sandbox roles %(user)s_1…%(user)s_%(count)s on %(host)s:%(port)s/%(db)s: "
                "no privilege beyond CONNECT and TEMPORARY, no access to the Odoo database.",
                count=settings["roles"],
                user=settings["user"],
                host=settings["host"],
                port=settings["port"],
                db=settings["database"],
            )
        ]
        return ("warning" if warnings else "success"), warnings + lines + sorted(set(notes))

    def _sql_transport_warnings(self, settings):
        """A remote sandbox must be reached with sslmode verify-full."""
        if sandbox_is_local(settings["host"]) or settings["sslmode"] == "verify-full":
            return []
        return [
            self.env._(
                "The sandbox %(host)s is not on this host and its connection uses sslmode %(mode)s: "
                "copied rows can be read or changed on the network. Use verify-full.",
                host=settings["host"],
                mode=settings["sslmode"],
            )
        ]

    def _sql_check_role(self, connection, odoo_db, notes):
        _ = self.env._
        cursor = connection.cursor()
        problems = []

        def value(query, params=None):
            cursor.execute(query, params)
            return cursor.fetchone()[0]

        cursor.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolconnlimit"
            " FROM pg_roles WHERE rolname = current_user"
        )
        superuser, createrole, createdb, replication, bypassrls, connlimit = cursor.fetchone()
        for flag, label in (
            (superuser, "SUPERUSER"),
            (createrole, "CREATEROLE"),
            (createdb, "CREATEDB"),
            (replication, "REPLICATION"),
            (bypassrls, "BYPASSRLS"),
        ):
            if flag:
                problems.append(_("the role has %(attribute)s.", attribute=label))
        if connlimit != 1:
            problems.append(_("the role must have CONNECTION LIMIT 1."))
        memberships = value(
            "SELECT string_agg(g.rolname, ', ') FROM pg_auth_members m"
            " JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member"
            " WHERE r.rolname = current_user"
        )
        if memberships:
            problems.append(_("the role is a member of %(roles)s.", roles=memberships))
        if value("SELECT has_database_privilege(current_database(), 'CREATE')"):
            problems.append(_("the role may create schemas in the sandbox database."))
        if not value("SELECT has_database_privilege(current_database(), 'TEMPORARY')"):
            problems.append(_("the role may not create temporary tables in the sandbox database."))
        schemas = value(
            "SELECT string_agg(nspname, ', ') FROM pg_namespace"
            " WHERE has_schema_privilege(oid, 'CREATE') AND nspname !~ '^pg_(toast_)?temp_'"
        )
        if schemas:
            problems.append(_("the role may create objects in schemas %(schemas)s.", schemas=schemas))
        relations = value(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
            " AND n.nspname !~ '^pg_(toast_)?temp_'"
        )
        if relations:
            problems.append(_("the sandbox database is not empty (%(count)s relations).", count=relations))
        extensions = value("SELECT string_agg(extname, ', ') FROM pg_extension WHERE extname <> 'plpgsql'")
        if extensions:
            problems.append(_("the sandbox database has extensions %(names)s.", names=extensions))
        databases = value(
            "SELECT string_agg(datname, ', ') FROM pg_database WHERE datallowconn"
            " AND datname <> current_database() AND has_database_privilege(datname, 'CONNECT')"
        )
        if databases:
            problems.append(_("the role may connect to databases %(names)s.", names=databases))
        if value("SELECT count(*) FROM pg_database WHERE datname = %s", [odoo_db]):
            problems.append(
                _("the sandbox runs on the PostgreSQL instance that holds the Odoo database; use a separate instance.")
            )
        functions = value(
            "SELECT string_agg(f, ', ') FROM unnest(%s::text[]) f WHERE has_function_privilege(f, 'EXECUTE')",
            [
                [
                    "pg_read_file(text)",
                    "pg_read_binary_file(text)",
                    "pg_ls_dir(text)",
                    "pg_stat_file(text)",
                    "lo_import(text)",
                    "lo_export(oid,text)",
                ]
            ],
        )
        if functions:
            problems.append(_("the role may run %(names)s.", names=functions))
        cursor.execute("CREATE TEMPORARY TABLE o3p_mcp_check (x integer) ON COMMIT DROP")
        cursor.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        ssl, version = cursor.fetchone() or (False, None)
        notes.append(
            _("The sandbox connections are encrypted (%(version)s).", version=version)
            if ssl
            else _("The sandbox connections are not encrypted.")
        )
        if value("SELECT current_setting('temp_file_limit')") == "-1":
            notes.append(_("temp_file_limit is unlimited for the sandbox roles; consider ALTER ROLE … SET temp_file_limit."))
        return problems

    def _sql_can_reach_odoo(self, role):
        try:
            connection = psycopg2.connect(
                host=config.get("db_host") or None,
                port=config.get("db_port") or None,
                dbname=self.env.cr.dbname,
                user=role,
                password=sql_password(),
                connect_timeout=CONNECT_TIMEOUT,
                sslmode=config.get("db_sslmode") or "prefer",
            )
        except psycopg2.Error:
            return False
        connection.close()
        return True
