// Plan step E4: permission-scoped SQL through MCP (okf-mcp → okf-odoo's
// odoo_sql → o3p_mcp's `sql` operation → copies of the rows the user may
// read in a separate PostgreSQL sandbox). Every D4 user asks the same
// questions in SQL and the answers are compared with the ORM as that user
// (setup_sales.py); then a hostile salesperson tries every SQL shape that
// could count or list the other salesperson's orders, reach hidden tables or
// columns, escape the sandbox or exhaust it. A leak fails its check with the
// query that reproduces it.

export async function runE4(ctx) {
  const { check, info, assert, sales, ISSUER } = ctx;
  const { users: U, facts, truth } = sales;
  const json = (value) => JSON.stringify(value);
  const errors = []; // every SQL error message seen, scanned for hidden data at the end

  // One odoo_sql call through MCP: { ok, result, ms } or { ok: false, code, details }.
  async function sql(client, query, limit) {
    const started = performance.now();
    let answer;
    try {
      answer = await client.callTool({ name: "odoo_sql", arguments: { query, ...(limit ? { limit } : {}) } });
    } catch (error) {
      return { ok: false, code: "jsonrpc_error", message: error.message, ms: performance.now() - started };
    }
    const ms = performance.now() - started;
    const body = ctx.toolJson(answer);
    if (answer.isError) {
      const code = body?.code || (typeof body === "string" && body.startsWith("Input validation error") ? "schema" : "tool_error");
      if (body?.details?.message) errors.push({ query, message: body.details.message });
      return { ok: false, code, details: body?.details, ms };
    }
    return { ok: true, result: body, ms };
  }
  // The data API called directly with the resource-server credentials.
  async function direct(op, token, params) {
    const { client_id: id, client_secret: secret } = ctx.setup.introspection;
    const response = await fetch(`${ISSUER}/o3p_mcp/api/v1/${op}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Basic ${Buffer.from(`${id}:${secret}`).toString("base64")}` },
      body: JSON.stringify({ token, params }),
    });
    return { status: response.status, body: await response.json() };
  }
  const value = (answer) => (answer.ok ? answer.result.rows[0]?.[0] : `${answer.code} ${json(answer.details)}`);
  const rowsOf = (answer) => (answer.ok ? answer.result.rows : []);
  const repro = (query, who = "sp_a") => `as ${U[who].login}: odoo_sql ${json({ query })}`;

  // -------------------------------------------------------------------------
  // Setup and sign-in
  // -------------------------------------------------------------------------

  await check("E0", "Settings › MCP › SQL: the sandbox is configured and Check SQL sandbox reports no problem", async () => {
    assert(sales.sql_check?.type === "success", `${sales.sql_check?.type}: ${sales.sql_check?.message}`);
    return sales.sql_check.message;
  });

  const sessions = {};
  const keys = ["sp_a", "sp_b", "manager", "d_admin", "mcp_only"];
  await check("E1", "Every user with MCP SQL signs in with odoo.sql and is offered odoo_sql; the portal user gets no token", async () => {
    for (const key of keys) {
      const result = await ctx.signInAs(U[key].login);
      assert(result.outcome === "signed-in" && result.tokens.scope.split(" ").includes("odoo.sql"), `${key}: ${result.outcome} "${result.tokens?.scope}"`);
      const tools = (await result.client.listTools()).tools.map((tool) => tool.name);
      assert(tools.includes("odoo_sql"), `${key}: odoo_sql not listed`);
      sessions[key] = result;
    }
    const portal = await ctx.signInAs(ctx.setup.users.portal.login);
    assert(portal.outcome === "page" && portal.authorization.page.status === 403, `portal: ${portal.outcome}`);
    return "5 users with odoo.sql; portal → 403, no token";
  });
  if (!sessions.sp_a) return;
  const A = sessions.sp_a.client;

  await check("E2", "Without odoo.sql the tool is not offered and the data API refuses the sql operation", async () => {
    const result = await ctx.signInAs(U.sp_a.login, { scope: "okf.read odoo.read" });
    const tools = (await result.client.listTools()).tools.map((tool) => tool.name);
    const answer = await sql(result.client, "SELECT 1");
    const api = await direct("sql", result.tokens.access_token, { query: "SELECT 1" });
    await result.client.close();
    assert(!tools.includes("odoo_sql") && tools.includes("odoo_search"), `tools: ${tools.filter((n) => n.startsWith("odoo")).join(",")}`);
    assert(!answer.ok, "the call ran");
    assert(api.status === 403 && api.body.error?.code === "insufficient_scope", `data API → ${api.status} ${json(api.body)}`);
    return `odoo_sql absent, call → ${answer.code}, data API → insufficient_scope`;
  });

  await check("E2b", "An MCP user without the MCP SQL group is granted no odoo.sql and is not offered odoo_sql", async () => {
    const result = await ctx.signInAs(ctx.setup.users.editor2.login);
    assert(result.outcome === "signed-in", `sign-in: ${result.outcome}`);
    const scopes = result.tokens.scope.split(" ");
    const tools = (await result.client.listTools()).tools.map((tool) => tool.name);
    const api = await direct("sql", result.tokens.access_token, { query: "SELECT 1" });
    await result.client.close();
    assert(scopes.includes("odoo.read") && !scopes.includes("odoo.sql"), `granted "${result.tokens.scope}"`);
    assert(!tools.includes("odoo_sql") && tools.includes("odoo_search"), `tools: ${tools.filter((n) => n.startsWith("odoo")).join(",")}`);
    assert(api.status === 403 && api.body.error?.code === "insufficient_scope", `data API → ${api.status} ${json(api.body)}`);
    return `granted "${result.tokens.scope}"; odoo_sql absent; data API → insufficient_scope`;
  });

  // -------------------------------------------------------------------------
  // The same questions for every user, in SQL
  // -------------------------------------------------------------------------

  const close = (a, b) => Math.abs(Number(a) - Number(b)) < 0.005;
  const perUser = (id, name, fn) =>
    check(id, name, async () => {
      const notes = [];
      for (const key of keys) notes.push(`${key}: ${await fn(key, sessions[key].client, truth[key])}`);
      return notes.join("; ");
    });
  const noOrders = (key, answer) => {
    assert(!answer.ok && answer.details?.sqlstate === "42P01", `${key}: ${answer.ok ? `returned ${json(answer.result.rows).slice(0, 80)}` : answer.code}`);
    return "relation does not exist (42P01)";
  };

  await perUser("E3", "Counts and sums by salesperson equal the ORM's for that user", async (key, client, t) => {
    const answer = await sql(client, "SELECT user_id, count(*), sum(amount_total) FROM sale_order GROUP BY user_id ORDER BY user_id");
    if (!t.can_read_orders) return noOrders(key, answer);
    assert(answer.ok, `${key}: ${answer.code} ${json(answer.details)}`);
    const got = Object.fromEntries(answer.result.rows.map(([user, count, sum]) => [user === null ? "False" : String(user), [sum, count]]));
    assert(json(Object.keys(got).sort()) === json(Object.keys(t.by_user).sort()), `${key}: ${json(got)} vs ORM ${json(t.by_user)}`);
    for (const [user, [sum, count]] of Object.entries(t.by_user)) {
      assert(close(got[user][0], sum) && got[user][1] === count, `${key}: ${user} ${json(got[user])} vs ORM ${json([sum, count])}`);
    }
    const copied = answer.result.tables.find((table) => table.name === "sale_order")?.rows;
    assert(copied === t.order_ids.length, `${key}: ${copied} rows copied vs ${t.order_ids.length} readable`);
    return `${Object.keys(got).length} groups, ${copied} orders copied`;
  });

  await perUser("E4", "Counts and sums by month (user's time zone) equal the ORM's for that user", async (key, client, t) => {
    const answer = await sql(
      client,
      "SELECT to_char((date_order AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Rome', 'YYYY-MM') AS month, count(*), sum(amount_total) FROM sale_order GROUP BY 1 ORDER BY 1",
    );
    if (!t.can_read_orders) return noOrders(key, answer);
    assert(answer.ok, `${key}: ${answer.code} ${json(answer.details)}`);
    const got = Object.fromEntries(answer.result.rows.map(([month, count, sum]) => [month, [sum, count]]));
    assert(json(Object.keys(got).sort()) === json(Object.keys(t.by_month).sort()), `${key}: months ${json(Object.keys(got))}`);
    for (const [month, [sum, count]] of Object.entries(t.by_month)) {
      assert(close(got[month][0], sum) && got[month][1] === count, `${key}: ${month} ${json(got[month])} vs ORM ${json([sum, count])}`);
    }
    return `${Object.keys(got).length} months, Sept ${got["2026-09"]?.[1]} orders`;
  });

  await perUser("E5", "sale_order ↔ res_partner ↔ sale_order_line joins count only the rows the user may read", async (key, client, t) => {
    const answer = await sql(
      client,
      "SELECT count(*), sum(l.price_total), count(DISTINCT o.id), count(DISTINCT p.id) FROM sale_order_line l JOIN sale_order o ON o.id = l.order_id JOIN res_partner p ON p.id = o.partner_id",
    );
    if (!t.can_read_orders) return noOrders(key, answer);
    assert(answer.ok, `${key}: ${answer.code} ${json(answer.details)}`);
    const [lines, total, orders, partners] = answer.result.rows[0];
    assert(lines === t.line_count && close(total, t.line_total), `${key}: ${lines} lines / ${total} vs ORM ${t.line_count} / ${t.line_total}`);
    assert(orders === t.order_ids.length, `${key}: ${orders} orders with lines vs ${t.order_ids.length} readable`);
    assert(partners === new Set(t.partners_with_visible_orders).size, `${key}: ${partners} customers vs ${new Set(t.partners_with_visible_orders).size}`);
    const copied = Object.fromEntries(answer.result.tables.map((table) => [table.name, table.rows]));
    assert(copied.res_partner === t.partner_count, `${key}: res_partner copied ${copied.res_partner} vs ORM ${t.partner_count}`);
    return `${lines} lines, ${orders} orders, ${partners} customers (copied ${json(copied)})`;
  });

  await perUser("E6", "Many-to-many pairs (sale_order_tag_rel) hold only pairs whose order the user may read", async (key, client, t) => {
    const answer = await sql(client, "SELECT count(*) FROM sale_order_tag_rel");
    if (!t.can_read_orders) return noOrders(key, answer);
    assert(answer.ok && value(answer) === t.tag_pairs, `${key}: ${value(answer)} pairs vs ORM ${t.tag_pairs}`);
    return `${value(answer)} pairs`;
  });

  // -------------------------------------------------------------------------
  // The hostile salesperson
  // -------------------------------------------------------------------------

  const B = U.sp_b.id;
  const hidden = facts.hidden_from_a.join(", ");
  const onlyB = facts.only_b_customers.join(", ");
  const visible = truth.sp_a;
  const shapes = [
    ["filter on the other salesperson", `SELECT count(*) FROM sale_order WHERE user_id = ${B}`, 0],
    ["hidden ids", `SELECT count(*) FROM sale_order WHERE id IN (${hidden})`, 0],
    ["B-only customers", `SELECT count(*) FROM sale_order WHERE partner_id IN (${onlyB})`, 0],
    ["scalar subqueries", `SELECT (SELECT count(*) FROM sale_order WHERE user_id = ${B}) + (SELECT count(*) FROM sale_order_line WHERE order_id IN (${hidden}))`, 0],
    ["window function", "SELECT count(*) OVER () FROM sale_order LIMIT 1", visible.order_ids.length],
    ["CTE", `WITH o AS (SELECT * FROM sale_order) SELECT count(*) FROM o WHERE user_id = ${B}`, 0],
    ["recursive CTE over ids", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 2000) SELECT count(*) FROM r JOIN sale_order o ON o.id = r.n", visible.order_ids.length],
    ["UNION", `SELECT count(*) FROM (SELECT id FROM sale_order WHERE user_id = ${B} UNION SELECT order_id FROM sale_order_line WHERE order_id IN (${hidden})) u`, 0],
    ["EXISTS", `SELECT count(*) FROM res_partner p WHERE EXISTS (SELECT 1 FROM sale_order o WHERE o.partner_id = p.id AND o.user_id = ${B})`, 0],
    ["LATERAL per customer", "SELECT count(*) FROM res_partner p, LATERAL (SELECT count(*) AS c FROM sale_order o WHERE o.partner_id = p.id) x WHERE x.c > 0", new Set(visible.partners_with_visible_orders).size],
    ["anti-join of lines without visible orders", "SELECT count(*) FROM sale_order_line l LEFT JOIN sale_order o ON o.id = l.order_id WHERE o.id IS NULL", 0],
    ["max id gap", `SELECT count(*) FROM sale_order WHERE id > ${Math.max(...facts.a_order_ids)}`, visible.order_ids.filter((id) => id > Math.max(...facts.a_order_ids)).length],
    ["relation table, other's tag", `SELECT count(*) FROM sale_order_tag_rel WHERE tag_id = ${facts.tag_b}`, 0],
    ["relation table aggregate", `SELECT count(DISTINCT order_id) FROM sale_order_tag_rel WHERE order_id IN (${hidden})`, 0],
    ["sales report", `SELECT count(*) FROM sale_report WHERE user_id = ${B}`, 0],
    ["activities on hidden orders", `SELECT count(*) FROM mail_activity WHERE res_model = 'sale.order' AND res_id IN (${hidden})`, 0],
    ["catalog: temp tables", "SELECT count(*) FROM pg_class WHERE relname = 'sale_order' AND reltuples > (SELECT count(*) FROM sale_order)", 0],
    ["catalog: other sessions' tables", "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname LIKE 'pg_temp%' AND n.oid <> pg_my_temp_schema()", 0],
    ["pg_stats of the copy", `SELECT count(*) FROM pg_stats WHERE tablename = 'sale_order' AND most_common_vals::text LIKE '%${B}%' AND (SELECT count(*) FROM sale_order) >= 0`, 0],
    ["pg_stat_all_tables", "SELECT count(*) FROM pg_stat_all_tables WHERE relname LIKE 'sale%' AND n_tup_ins > (SELECT count(*) FROM sale_order)", 0],
    ["query_to_xml runs SQL on names the gate never saw", "SELECT query_to_xml('SELECT count(*) FROM sale_order WHERE user_id = " + B + "', true, true, '')", "42P01"],
  ];
  for (const [index, [label, query, expected]] of shapes.entries()) {
    const answer = await sql(A, query);
    const got = typeof expected === "string" ? answer.details?.sqlstate : value(answer);
    await check(`E7.${index + 1}`, `SQL shape "${label}" reveals nothing hidden`, async () => {
      assert(got === expected, `LEAK? ${repro(query)} → ${json(got)}, expected ${json(expected)} from the rows the user may read`);
      return `${json(got)}`;
    });
  }

  await check("E8", "Hidden tables and columns are unreachable for the salesperson and the administrator", async () => {
    const out = [];
    const tables = ["mail_followers", "payment_token", "payment_transaction", "ir_config_parameter", "mail_message", "ir_attachment", "o3p_mcp_token", "res_users_apikeys", "mail_tracking_value"];
    for (const key of ["sp_a", "d_admin"]) {
      for (const table of tables) {
        const answer = await sql(sessions[key].client, `SELECT count(*) FROM ${table}`);
        assert(!answer.ok && answer.details?.sqlstate === "42P01", `LEAK: ${repro(`SELECT count(*) FROM ${table}`, key)} → ${value(answer)}`);
      }
      const password = await sql(sessions[key].client, "SELECT password FROM res_users");
      assert(!password.ok && password.details?.sqlstate === "42703", `LEAK: ${repro("SELECT password FROM res_users", key)} → ${value(password)}`);
      const star = await sql(sessions[key].client, "SELECT * FROM res_users LIMIT 1");
      const sensitive = star.ok ? star.result.columns.map((c) => c.name).filter((name) => /password|secret|token|api_key|totp/.test(name)) : [];
      assert(star.ok && sensitive.length === 0, `LEAK: SELECT * FROM res_users exposes ${sensitive.join(",")}`);
      out.push(`${key}: ${tables.length} tables 42P01, password 42703, res_users.* without secrets`);
    }
    for (const column of ["access_token", "signature"]) {
      const answer = await sql(A, `SELECT ${column} FROM sale_order LIMIT 1`);
      assert(!answer.ok && answer.details?.sqlstate === "42703", `LEAK: ${repro(`SELECT ${column} FROM sale_order LIMIT 1`)} → ${value(answer)}`);
    }
    const whole = await sql(A, "SELECT to_jsonb(o.*) FROM sale_order o LIMIT 1");
    const keysOfRow = whole.ok ? Object.keys(whole.result.rows[0][0]) : [];
    assert(whole.ok && !keysOfRow.some((name) => ["access_token", "signature"].includes(name)), `LEAK: whole row carries ${keysOfRow.filter((n) => /token|signature/.test(n)).join(",")}`);
    out.push(`sale_order.access_token and .signature → 42703, not in whole rows (${keysOfRow.length} columns)`);
    const restricted = truth.sp_a.restricted_columns.res_partner || [];
    for (const column of restricted) {
      const answer = await sql(A, `SELECT ${column} FROM res_partner LIMIT 1`);
      assert(!answer.ok && answer.details?.sqlstate === "42703", `LEAK: ${repro(`SELECT ${column} FROM res_partner LIMIT 1`)} (field the salesperson may not read) → ${value(answer)}`);
    }
    out.push(`restricted res_partner columns for sp_a (${restricted.join(", ")}) → 42703`);
    return out.join("; ");
  });

  await check("E9", "A user without sales rights sees no sale_order rows by any join", async () => {
    const client = sessions.mcp_only.client;
    const queries = [
      "SELECT count(*) FROM sale_order",
      "SELECT count(*) FROM res_partner p JOIN sale_order o ON o.partner_id = p.id",
      "SELECT count(*) FROM sale_order_line",
      "SELECT count(*) FROM sale_order_tag_rel",
      "SELECT count(*) FROM sale_report",
    ];
    for (const query of queries) {
      const answer = await sql(client, query);
      assert(!answer.ok && answer.details?.sqlstate === "42P01", `LEAK: ${repro(query, "mcp_only")} → ${value(answer)}`);
    }
    return `${queries.length} queries → 42P01`;
  });

  await check("E10", "Statements other than one SELECT, sandbox escapes and system functions are refused", async () => {
    const cases = [
      ["SELECT 1; SELECT 2", "0A000"],
      ["SELECT * INTO stolen FROM sale_order", "0A000"],
      ["SELECT id FROM sale_order FOR UPDATE", "0A000"],
      ["COPY sale_order TO STDOUT", "0A000"],
      ["DO $$ BEGIN PERFORM 1; END $$", "0A000"],
      ["EXPLAIN SELECT * FROM sale_order", "0A000"],
      ["CREATE TABLE x (y int)", "0A000"],
      ["SELECT set_config('role', 'sandbox_admin', false)", "42501"],
      ["SELECT set_config('transaction_read_only', 'off', false)", "25001"],
      ["SELECT pg_read_file('/etc/passwd')", "42501"],
      ["SELECT pg_ls_dir('.')", "42501"],
      ["SELECT lo_import('/etc/passwd')", "42501"],
      ["SELECT current_setting('data_directory')", "42501"],
      ["SELECT * FROM dblink('dbname=o3p_e2e', 'select 1') AS t(x int)", "42883"],
      ["SELECT count(*) FROM o3p_e2e.public.sale_order", "0A000"],
      ["SELECT count(*) FROM public.sale_order", "42501"],
    ];
    const got = [];
    for (const [query, sqlstate] of cases) {
      const answer = await sql(A, query);
      assert(!answer.ok && answer.details?.sqlstate === sqlstate, `${query} → ${answer.ok ? json(answer.result.rows).slice(0, 80) : `${answer.code} ${json(answer.details)}`}`);
      got.push(sqlstate);
    }
    return `${cases.length} refused (${[...new Set(got)].join(", ")})`;
  });

  await check("E11", "Errors carry sqlstate, position and PostgreSQL's message, and never hidden data", async () => {
    const column = await sql(A, "SELECT nope FROM sale_order");
    assert(column.details?.sqlstate === "42703" && column.details.position === 8 && /column "nope" does not exist/.test(column.details.message), json(column.details));
    const syntax = await sql(A, "SELEC 1");
    assert(syntax.details?.sqlstate === "42601" && syntax.details.position === 1, json(syntax.details));
    // Data in errors: casts fail on the first row, which can only be a readable one.
    await sql(A, "SELECT name::int FROM sale_order ORDER BY id DESC");
    await sql(A, `SELECT name::int FROM res_partner WHERE id IN (${onlyB}, ${facts.only_a_customers.join(", ")}) ORDER BY id DESC`);
    await sql(A, `SELECT 1 / (amount_total - amount_total) FROM sale_order WHERE user_id = ${B}`);
    await sql(A, "SELECT (SELECT name FROM sale_order)::int");
    const secrets = [...facts.hidden_order_names, ...facts.b_only_customer_names];
    const leaked = errors.filter((entry) => secrets.some((secret) => entry.message.includes(secret)));
    assert(leaked.length === 0, `LEAK: ${leaked.map((entry) => `${repro(entry.query)} → ${entry.message}`).join("; ")}`);
    const bounded = errors.every((entry) => entry.message.length <= 500);
    assert(bounded, "a message is longer than 500 characters");
    return `${errors.length} error messages scanned for ${secrets.length} hidden names; e.g. ${json(column.details)}`;
  });

  await check("E12", "Cumulative statistics of the sandbox do not reveal how many rows another user's query copied", async () => {
    const statQuery = "SELECT tup_inserted FROM pg_stat_database WHERE datname = current_database()";
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const deltas = {};
    for (const key of ["sp_b", "manager"]) {
      const before = value(await sql(A, statQuery));
      await sql(sessions[key].client, "SELECT count(*) FROM sale_order");
      await sleep(1200);
      deltas[key] = value(await sql(A, statQuery)) - before;
    }
    assert(deltas.sp_b === deltas.manager, `tup_inserted grew by ${deltas.sp_b} around B's query (30 rows) and ${deltas.manager} around the manager's (61 rows)`);
    const concurrent = sql(sessions.sp_b.client, "SELECT count(*), pg_sleep(1.5) FROM sale_order");
    await sleep(500);
    const activity = await sql(A, "SELECT query FROM pg_stat_activity WHERE usename IS DISTINCT FROM current_user");
    await concurrent;
    const visibleQueries = rowsOf(activity).map(([query]) => query).filter((query) => query && query !== "<insufficient privilege>");
    assert(visibleQueries.length === 0, `LEAK: pg_stat_activity shows ${json(visibleQueries)}`);
    return `tup_inserted delta ${deltas.sp_b} for both (catalog rows only); other sessions' queries "<insufficient privilege>"`;
  });

  await check("E13", "Results and limits: truncation, a too-large result, and an over-long query", async () => {
    const page = await sql(A, "SELECT id, name FROM res_partner ORDER BY id", 1000);
    assert(page.ok && page.result.rows.length === 1000 && page.result.truncated === true, `truncation: ${page.ok ? page.result.rows.length : page.code}`);
    const big = await sql(A, "SELECT repeat('x', 300000) FROM generate_series(1, 5)");
    assert(!big.ok && big.code === "result_too_large", `big result → ${big.code}`);
    const long = await sql(A, `SELECT 1 ${" ".repeat(20001)}`);
    assert(!long.ok, "a 20 KB query ran");
    return `truncated at 1000 rows; 1.5 MB → result_too_large; over-long query → ${long.code}`;
  });

  await check("E14", "Concurrent queries: the data API refuses a user's third query and a fifth user beyond the four sandbox roles (429); through okf-mcp the refused queries wait for a free slot and complete", async () => {
    const slow = "SELECT count(*), pg_sleep(2) FROM sale_order";
    const queryOf = (key) => (key === "mcp_only" ? "SELECT pg_sleep(2)" : slow);
    const codeOf = (answer) => (answer.status === 200 ? "ok" : answer.body?.error?.code || String(answer.status));
    const own = (await Promise.all([0, 1, 2].map(() => direct("sql", sessions.sp_a.tokens.access_token, { query: slow })))).map(codeOf);
    assert(own.filter((code) => code === "ok").length === 2 && own.includes("too_many_requests"), `data API, one user: ${own.join(",")}`);
    const many = (await Promise.all(keys.map((key) => direct("sql", sessions[key].tokens.access_token, { query: queryOf(key) })))).map(codeOf);
    assert(many.filter((code) => code === "ok").length === 4 && many.includes("too_many_requests"), `data API, five users: ${many.join(",")}`);
    // okf-odoo waits for a free slot and sends a refused query again.
    const viaMcp = await Promise.all([0, 1, 2].map(() => sql(A, slow)));
    const mcpCodes = viaMcp.map((answer) => (answer.ok ? "ok" : answer.code));
    const slowest = Math.max(...viaMcp.map((answer) => answer.ms));
    assert(mcpCodes.every((code) => code === "ok"), `okf-mcp, one user: ${mcpCodes.join(",")}`);
    assert(slowest >= 3500, `okf-mcp, one user: the third query did not wait for a slot (slowest ${Math.round(slowest)} ms)`);
    const manyMcp = (await Promise.all(keys.map((key) => sql(sessions[key].client, queryOf(key))))).map((answer) => (answer.ok ? "ok" : answer.code));
    assert(manyMcp.every((code) => code === "ok"), `okf-mcp, five users: ${manyMcp.join(",")}`);
    return `data API: one user ${own.join(",")}, five users ${many.join(",")}; okf-mcp: one user ${mcpCodes.join(",")} (slowest ${(slowest / 1000).toFixed(1)} s), five users ${manyMcp.join(",")}`;
  });

  await check("E15", "Long-running queries stop at the time limit (here lowered to 3 s), also after raising statement_timeout", async () => {
    const admin = await ctx.adminSession();
    const out = [];
    try {
      await admin.saveSettings({ o3p_mcp_api_timeout_seconds: 3 });
      for (const query of [
        "SELECT pg_sleep(30)",
        "SELECT set_config('statement_timeout', '0', false), pg_sleep(30)",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
        "SELECT count(*) FROM generate_series(1, 3000000000)",
      ]) {
        const answer = await sql(A, query);
        assert(!answer.ok && ["timeout", "query_error"].includes(answer.code) && answer.ms < 6000, `${query} → ${answer.ok ? "ran" : answer.code} after ${Math.round(answer.ms)} ms`);
        out.push(`${answer.code === "timeout" ? "timeout" : answer.details?.sqlstate} ${(answer.ms / 1000).toFixed(1)} s`);
      }
    } finally {
      await admin.saveSettings({ o3p_mcp_api_timeout_seconds: 15 });
    }
    const after = await sql(A, "SELECT 1");
    assert(after.ok, "the sandbox did not recover");
    return out.join(", ");
  });

  // -------------------------------------------------------------------------
  // Latency
  // -------------------------------------------------------------------------

  const pct = (list, p) => {
    const sorted = [...list].sort((a, b) => a - b);
    return Math.round(sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)]);
  };
  async function measure(client, query, runs) {
    const times = [];
    for (let run = 0; run < runs; run += 1) {
      const answer = await sql(client, query);
      if (!answer.ok) throw new Error(`${query} → ${answer.code} ${json(answer.details)}`);
      times.push(answer.ms);
    }
    return `median ${pct(times, 50)} ms p95 ${pct(times, 95)} ms`;
  }
  await check("E16", "SQL latency as the restricted salesperson (15 runs each, through okf-mcp)", async () => {
    const typical = [
      ["count", "SELECT count(*), sum(amount_total) FROM sale_order WHERE state = 'sale'"],
      ["by month", "SELECT date_trunc('month', date_order) AS m, sum(amount_total) FROM sale_order GROUP BY 1 ORDER BY 1"],
      ["top customers", "SELECT p.name, sum(o.amount_total) FROM sale_order o JOIN res_partner p ON p.id = o.partner_id GROUP BY p.name ORDER BY 2 DESC LIMIT 5"],
      ["3-table join", "SELECT p.city, count(DISTINCT o.id), sum(l.price_total) FROM sale_order_line l JOIN sale_order o ON o.id = l.order_id JOIN res_partner p ON p.id = o.partner_id GROUP BY p.city"],
      ["window", "SELECT name, amount_total, rank() OVER (ORDER BY amount_total DESC) FROM sale_order LIMIT 10"],
    ];
    const parts = [];
    for (const [label, query] of typical) parts.push(`${label} ${await measure(A, query, 15)}`);
    return parts.join("; ");
  });
  await check("E17", `SQL latency as the administrator on the largest table (${facts.largest_table.table}, ${facts.largest_table.rows} rows; 10 runs each)`, async () => {
    const admin = sessions.d_admin.client;
    const parts = [];
    for (const [label, query] of [
      ["count", "SELECT count(*) FROM res_partner"],
      ["group by city", "SELECT city, count(*) FROM res_partner GROUP BY city ORDER BY 2 DESC"],
      ["distinct emails", "SELECT count(DISTINCT email) FROM res_partner"],
      ["3 columns, all rows copied", "SELECT max(name), max(email), max(city) FROM res_partner"],
    ]) parts.push(`${label} ${await measure(admin, query, 10)}`);
    return parts.join("; ");
  });

  info("E", `${errors.length} SQL error messages collected; none contains a hidden order or customer name (E11)`);
  for (const session of Object.values(sessions)) await session.client.close();
}
