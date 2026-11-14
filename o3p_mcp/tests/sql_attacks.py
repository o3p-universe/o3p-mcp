"""Escape attempts against the sql operation (the E4 suite).

Each attack is ``(id, sql, expect)`` or ``(id, sql, expect, check)``:

- ``expect``: ``"rows"`` (200), ``"error"`` (422 query_error),
  ``"refused"`` (422 query_error from the statement gate, sqlstate 0A000 or
  42601), ``"timeout"`` (504), or a set of those;
- ``check(case, body)``: extra assertions on the decoded answer.

``{db}`` is replaced by the name of Odoo's database and ``{hidden}`` by the
id of a partner the attacking user may not read. Whatever the outcome, the
test asserts that no secret marker appears anywhere in the answer.

Ported from the E0 spike's attack matrix (75 attempts against 9 designs);
attempts that only applied to other designs (same-session role switching
into Odoo's database, rewriting) are kept: here they must simply fail.
"""


def no_rows_with(value):
    def check(case, body):
        rows = body.get("result", {}).get("rows", [])
        case.assertFalse([row for row in rows if value in row], f"{value!r} in {rows[:5]}")

    return check


def sandbox_only(case, body):
    """Catalog answers may only describe the sandbox, never Odoo's instance."""
    text = str(body.get("result", {}).get("rows", []))
    case.assertNotIn(case.env.cr.dbname, text)


def roles_are_sandbox_roles(case, body):
    rows = body.get("result", {}).get("rows", [])
    names = {row[0] for row in rows}
    case.assertFalse({"odoo", case.env.cr.dbname} & names, names)


def q2x(inner):
    return f"query_to_xml($q${inner}$q$, true, false, '')"


ATTACKS = [
    # --- names ------------------------------------------------------------
    ("hidden-rows", "SELECT name, comment FROM res_partner", {"rows", "error"}),
    ("hidden-row-by-id", "SELECT name FROM res_partner WHERE id = {hidden}", "rows",
     lambda case, body: case.assertEqual(body["result"]["rows"], [])),
    ("password-column", "SELECT login, password FROM res_users", "error"),
    ("restricted-column", "SELECT comment FROM res_partner", "error"),
    ("denied-model", "SELECT key, value FROM ir_config_parameter", "error"),
    ("denied-model-tokens", "SELECT * FROM o3p_mcp_token", "error"),
    ("schema-qualified", "SELECT login, password FROM public.res_users", "error"),
    ("quoted-qualified", 'SELECT name FROM "public"."res_partner"', "error"),
    ("catalog-qualified", "SELECT name FROM {db}.public.res_partner", "error"),
    ("only-inheritance", "SELECT name FROM ONLY public.res_partner", "error"),
    ("table-shorthand", "TABLE public.res_users", "error"),
    ("unicode-ident", 'SELECT password FROM U&"res\\0075sers"', "error"),
    ("temp-schema-qualified", "SELECT name FROM pg_temp.res_partner WHERE id = {hidden}", "rows",
     lambda case, body: case.assertEqual(body["result"]["rows"], [])),
    ("m2m-hidden-side", "SELECT partner_id FROM res_partner_res_partner_category_rel "
     "UNION ALL SELECT id FROM res_partner", "rows",
     lambda case, body: case.assertNotIn([case.hidden2.id], body["result"]["rows"])),
    ("whole-row", "SELECT p FROM res_partner p", "rows"),
    ("row-to-json", "SELECT row_to_json(p) FROM res_partner p", "rows"),
    # --- role, session and configuration ----------------------------------
    ("set_config-role", "SELECT set_config('role', 'sandbox_admin', true), "
     + q2x("select * from pg_authid"), "error"),
    ("set_config-session_auth", "SELECT set_config('session_authorization', 'sandbox_admin', true)", "error"),
    ("set_config-role-odoo", "SELECT set_config('role', 'odoo', true)", "error"),
    ("RESET-ROLE", "RESET ROLE; SELECT password FROM public.res_users", "refused"),
    ("SET-ROLE", "SET ROLE sandbox_admin; SELECT 1", "refused"),
    ("SET-SESSION-AUTH", "SET SESSION AUTHORIZATION sandbox_admin; SELECT 1", "refused"),
    ("set_config-search_path", "SELECT set_config('search_path', 'public, pg_temp', true), "
     + q2x("select name from res_partner"), {"rows", "error"}),
    ("DO-block", "DO $$BEGIN RAISE NOTICE 'x'; END$$", "refused"),
    # --- functions that run SQL -------------------------------------------
    ("query_to_xml", "SELECT " + q2x("select password from public.res_users"), "error"),
    # Functions that run SQL see the user's copy and nothing else.
    ("query_to_xml-own", "SELECT (SELECT count(*) FROM res_partner), " + q2x("select id from res_partner"), "rows"),
    ("table_to_xml", "SELECT table_to_xml('public.res_users', true, false, '')", "error"),
    ("cursor_to_xml", "DECLARE c CURSOR FOR SELECT 1; SELECT cursor_to_xml('c', 10, false, false, '')", "refused"),
    ("database_to_xml", "SELECT database_to_xml(true, false, '')", {"rows", "error"}),
    ("schema_to_xml", "SELECT schema_to_xml('public', true, false, '')", {"rows", "error"}),
    ("ts_stat", "SELECT * FROM ts_stat($q$select to_tsvector(name) from res_partner$q$)", {"rows", "error"}),
    ("dblink", "SELECT * FROM dblink('dbname={db} user=odoo', 'select password from res_users') AS t(p text)",
     "error"),
    ("postgres_fdw", "CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw OPTIONS (dbname '{db}')", "refused"),
    # --- files, large objects, COPY ---------------------------------------
    ("pg_read_file", "SELECT pg_read_file('/etc/passwd')", "error"),
    ("pg_read_binary_file-env",
     "SELECT convert_from(pg_read_binary_file('/proc/self/environ'), 'SQL_ASCII')", "error"),
    ("pg_ls_dir", "SELECT pg_ls_dir('.')", "error"),
    ("pg_stat_file", "SELECT pg_stat_file('postgresql.conf')", "error"),
    ("lo_get", "SELECT lo_get(1)", "error"),
    ("lo_import", "SELECT lo_import('/etc/passwd')", "error"),
    ("lo_from_bytea", "SELECT lo_from_bytea(0, 'x')", "error"),
    ("COPY-to-stdout", "COPY (SELECT 1) TO STDOUT", "refused"),
    ("COPY-from-program", "COPY res_partner FROM PROGRAM 'id'", "refused"),
    # --- catalogs and statistics ------------------------------------------
    ("pg_stats-values", "SELECT tablename, attname, most_common_vals::text, histogram_bounds::text FROM pg_stats",
     "rows"),
    ("pg_class", "SELECT relname, relnamespace::regnamespace::text, reltuples FROM pg_class "
     "WHERE relkind IN ('r', 'v', 'm')", "rows", sandbox_only),
    ("pg_stat-tables", "SELECT schemaname, relname, n_live_tup FROM pg_stat_all_tables", "rows", sandbox_only),
    ("pg_sequences", "SELECT * FROM pg_sequences", "rows",
     lambda case, body: case.assertEqual(body["result"]["rows"], [])),
    ("relation-size", "SELECT pg_total_relation_size('public.res_partner')", "error"),
    ("column-names", "SELECT attname FROM pg_attribute WHERE attrelid = 'public.res_users'::regclass", "error"),
    ("information_schema", "SELECT table_schema, table_name FROM information_schema.tables", "rows", sandbox_only),
    ("pg_stat_activity", "SELECT usename, application_name, query FROM pg_stat_activity", "rows", sandbox_only),
    ("data_directory", "SELECT current_setting('data_directory')", "error"),
    ("hba-rules", "SELECT * FROM pg_hba_file_rules", "error"),
    ("cluster-database-names", "SELECT datname FROM pg_database", "rows", sandbox_only),
    ("cluster-db-activity", "SELECT datname, tup_inserted FROM pg_stat_database", "rows", sandbox_only),
    ("cluster-db-size", "SELECT pg_database_size('{db}')", "error"),
    ("cluster-role-names", "SELECT rolname FROM pg_roles", "rows", roles_are_sandbox_roles),
    # --- time and resources -----------------------------------------------
    ("pg_sleep", "SELECT pg_sleep(30)", "timeout"),
    ("recursive-cte", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
     "timeout"),
    ("cross-join-blowup",
     "SELECT count(*) FROM generate_series(1, 100000) a, generate_series(1, 100000) b", "timeout"),
    ("SET-timeout-0", "SET statement_timeout = 0; SELECT pg_sleep(30)", "refused"),
    ("set_config-timeout-0", "SELECT set_config('statement_timeout', '0', false); SELECT pg_sleep(30)", "refused"),
    ("set_config-timeout-same-statement", "SELECT set_config('statement_timeout', '0', true), pg_sleep(30)",
     "timeout"),
    ("terminate-backends", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
     "AND backend_type = 'client backend'", {"rows", "error"}),
    ("advisory-lock", "SELECT pg_advisory_lock(1)", "rows"),
    ("notify", "SELECT pg_notify('imbus', 'o3p')", "rows"),
    # --- statements and transactions --------------------------------------
    ("two-statements", "SELECT 1; SELECT password FROM public.res_users", "refused"),
    ("comment-then-statement", "SELECT 1 -- x\n; SELECT password FROM public.res_users", "refused"),
    ("block-comment", "/* ; */ SELECT password FROM public.res_users /* ; */", "error"),
    ("COMMIT-escape", "COMMIT; SELECT password FROM public.res_users", "refused"),
    ("ROLLBACK-escape", "ROLLBACK; SELECT 1", "refused"),
    ("PREPARE-EXECUTE", "PREPARE p AS SELECT 1; EXECUTE p", "refused"),
    ("EXECUTE-alone", "EXECUTE p", "refused"),
    ("SELECT-INTO", "SELECT * INTO TEMP leak FROM res_partner", "refused"),
    ("FOR-UPDATE", "SELECT id FROM res_partner FOR UPDATE", "refused"),
    ("FOR-SHARE-subquery", "SELECT * FROM (SELECT id FROM res_partner FOR SHARE) s", "refused"),
    ("writable-CTE-real", "WITH d AS (DELETE FROM public.res_partner RETURNING name) SELECT * FROM d", "error"),
    ("writable-CTE-copy", "WITH d AS (DELETE FROM res_partner RETURNING name) SELECT count(*) FROM d",
     {"rows", "error"}),
    ("create-table", "CREATE TABLE x (a int)", "refused"),
    ("temp-collision", "COMMIT; CREATE TEMP TABLE res_partner (name text)", "refused"),
    ("empty-statement", ";", "refused"),
    ("unterminated", "SELECT 'x", "refused"),
    # --- planner side channels: hidden rows are not in the sandbox ---------
    ("pushdown-error-leak",
     "SELECT id FROM res_partner WHERE (CASE WHEN id = {hidden} THEN name::int ELSE 0 END) = 1", "rows"),
    ("pushdown-div0-oracle",
     "SELECT count(*) FROM res_partner WHERE 1 / (CASE WHEN id = {hidden} THEN 0 ELSE 1 END) = 1", "rows"),
    # --- rewriting tricks (design 3), harmless here -----------------------
    ("cte-scope-confusion", "SELECT b.login, b.password FROM "
     "(WITH res_users AS (SELECT 1 AS x) SELECT x FROM res_users) a, res_users b", "error"),
    ("cte-shadow-outer", "WITH res_partner AS (SELECT 1 AS id) SELECT * FROM public.res_partner", "error"),
    ("regclass-cast", "SELECT 'public.res_users'::regclass::oid", "error"),
    ("rows-from-function", "SELECT * FROM ROWS FROM (" + q2x("select password from public.res_users")
     + ") AS t(x)", "error"),
    ("lateral-subquery", "SELECT x.* FROM generate_series(1, 2) g, "
     "LATERAL (SELECT password FROM public.res_users) x", "error"),
    # PostgreSQL does not load external DTDs: the document comes back as is.
    ("xml-external-entity",
     "SELECT xmlparse(document '<!DOCTYPE a SYSTEM \"file:///etc/passwd\"><a>&x;</a>')", {"rows", "error"}),
]
