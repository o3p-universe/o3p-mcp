// Plan step D4: the Odoo data tools end to end and under attack. Every user
// signs in with the real OAuth flow and asks the same questions through MCP
// tools/call (okf-mcp → okf-odoo tool pack → o3p_mcp data API → Odoo as the
// user); the answers are compared with what Odoo's ORM gives that user
// (setup_sales.py). Then a hostile salesperson tries to learn what Odoo's
// rights hide. A leak is a failing check whose detail is the reproduction.
import fs from "node:fs";
import path from "node:path";

import { listFiles } from "./lib/project.mjs";

const SCHEMA_CODES = new Set(["invalid_arguments", "schema"]);
const FIXED_MESSAGES = new Set([
  "The parameters are not valid.",
  "A requested field is unknown, hidden or not readable.",
  "The domain is not valid, or uses a field that is unknown, hidden or not readable.",
  "The order is not valid: use 'field' or 'field desc' items on sortable readable fields.",
  "limit or offset is out of range.",
  "ids must be a list of 1 to 100 record ids.",
  "The operation does not take one of the parameters.",
  "groupby is not valid: use groupable readable fields; date fields need a granularity (day, week, month, quarter, year, or hour for datetimes).",
  "aggregates are not valid: use __count, field:sum|avg|min|max on numeric readable fields, or field:count|count_distinct on stored readable fields.",
  "The order is not valid: use groupby or aggregate items with asc or desc.",
  "Client authentication failed.",
  "The access token is not valid.",
  "The access token does not allow this operation.",
  "You are not allowed to read this.",
  "Unknown operation.",
  "Unknown model.",
  "The result is too large; ask for fewer records or fields.",
  "The query could not be run.",
  "The query took too long.",
]);

export async function runD4(ctx) {
  const { check, info, assert, setup, sales, oauth, ISSUER, MCP_URL } = ctx;
  const { users, facts, truth } = sales;
  const U = users;
  const json = (value) => JSON.stringify(value);

  // One MCP tool call: { ok, result } or { ok: false, code }. A schema
  // refusal by the MCP server is "schema"; a pack error carries its code.
  async function call(client, name, args) {
    const started = performance.now();
    let answer;
    try {
      answer = await client.callTool({ name, arguments: args });
    } catch (error) {
      return { ok: false, code: "jsonrpc_error", message: error.message, ms: performance.now() - started };
    }
    const ms = performance.now() - started;
    const body = ctx.toolJson(answer);
    if (answer.isError) {
      const code =
        body?.code || (typeof body === "string" && body.startsWith("Input validation error") ? "schema" : "tool_error");
      return { ok: false, code, body, ms };
    }
    return { ok: true, result: body, ms };
  }
  const repro = (tool, args, who = "sp_a") => `as ${U[who].login}: ${tool} ${json(args)}`;

  // The data API called directly with the resource-server credentials: what
  // a compromised or buggy MCP server could send, past the pack's schemas.
  async function direct(op, token, params, authorization) {
    const { client_id: id, client_secret: secret } = setup.introspection;
    const response = await fetch(`${ISSUER}/o3p_mcp/api/v1/${op}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(authorization !== null
          ? { authorization: authorization || `Basic ${Buffer.from(`${id}:${secret}`).toString("base64")}` }
          : {}),
      },
      body: JSON.stringify({ token, params }),
    });
    const text = await response.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
    return { status: response.status, body };
  }

  // -------------------------------------------------------------------------
  // Sign-in of every user
  // -------------------------------------------------------------------------

  const sessions = {};
  const keys = ["sp_a", "sp_b", "manager", "d_admin", "mcp_only"];
  await check("D0", "Every D4 user signs in with a standard MCP client and gets odoo.read; the portal user gets no token", async () => {
    const granted = [];
    for (const key of keys) {
      const result = await ctx.signInAs(U[key].login);
      assert(result.outcome === "signed-in", `${key}: ${result.outcome}`);
      assert(result.tokens.scope.split(" ").includes("odoo.read"), `${key} granted "${result.tokens.scope}"`);
      sessions[key] = result;
      granted.push(`${key} "${result.tokens.scope}"`);
    }
    const portal = await ctx.signInAs(setup.users.portal.login);
    assert(portal.outcome === "page" && portal.authorization.page.status === 403, `portal: ${portal.outcome}`);
    return `${granted.join("; ")}; portal → 403, no token`;
  });
  if (!sessions.sp_a) return;
  const A = sessions.sp_a.client;
  const tokenOf = (key) => sessions[key].tokens.access_token;

  await check("D0b", "Odoo data tools are listed after the core tools for a signed-in user", async () => {
    const tools = (await A.listTools()).tools.map((tool) => tool.name);
    const odoo = ["odoo_find_models", "odoo_describe_model", "odoo_search", "odoo_read", "odoo_group"];
    assert(odoo.every((name) => tools.includes(name)), `listed: ${tools.filter((n) => n.startsWith("odoo")).join(",")}`);
    const firstPack = tools.indexOf("odoo_find_models");
    assert(tools.slice(firstPack).every((name) => name.startsWith("odoo_")), "pack tools interleaved with core tools");
    const producerTools = tools.filter((name) => name.includes("producer"));
    const preview = await call(A, "okf_preview_producer", { producer: "odoo" });
    assert(producerTools.length === 0 && !preview.ok, `a plain MCP user sees ${producerTools.join(", ")}; preview → ${preview.ok ? "ran" : preview.code}`);
    return `${odoo.join(", ")}; producer tools not offered without okf.write (preview → ${preview.code})`;
  });

  // -------------------------------------------------------------------------
  // The same questions for every user
  // -------------------------------------------------------------------------

  const perUser = async (id, name, fn) =>
    check(id, name, async () => {
      const notes = [];
      for (const key of keys) notes.push(`${key}: ${await fn(key, sessions[key].client, truth[key])}`);
      return notes.join("; ");
    });

  await perUser("D1", "Find models: sale.order is offered exactly to the users who may read it", async (key, client, t) => {
    const answer = await call(client, "odoo_find_models", { query: "sale" });
    assert(answer.ok, `${key}: ${answer.code}`);
    const listed = answer.result.models.some((model) => model.model === "sale.order");
    assert(listed === t.can_read_orders, `${key}: sale.order listed=${listed}, ORM read access=${t.can_read_orders}`);
    return `${answer.result.models.length} models${listed ? " incl. sale.order" : ", no sale.order"}`;
  });

  await perUser("D2", "Describe sale.order: the user's readable fields, never restricted or hidden ones", async (key, client, t) => {
    const answer = await call(client, "odoo_describe_model", { model: "sale.order" });
    if (!t.can_read_orders) {
      assert(!answer.ok && answer.code === "access_denied", `${key}: ${answer.ok ? "described" : answer.code}`);
      return "access_denied";
    }
    assert(answer.ok, `${key}: ${answer.code}`);
    const names = new Set(answer.result.fields.map((field) => field.name));
    const { restricted, denied, all } = t.sale_order_fields;
    const exposed = [...restricted, ...denied].filter((name) => names.has(name));
    assert(exposed.length === 0, `${key}: exposes ${exposed.join(",")}`);
    const missing = all.filter((name) => !restricted.includes(name) && !denied.includes(name) && !names.has(name));
    assert(missing.length === 0, `${key}: missing ${missing.join(",")}`);
    return `${names.size} fields (hidden: ${[...restricted, ...denied].join(",")})`;
  });

  await perUser("D3", "Search their own orders: the same ids and count the ORM gives the user", async (key, client, t) => {
    const args = { model: "sale.order", domain: [["user_id", "=", U[key].id]], fields: ["name", "amount_total", "user_id"], limit: 200, order: "id" };
    const answer = await call(client, "odoo_search", args);
    if (!t.can_read_orders) {
      assert(!answer.ok && answer.code === "access_denied", `${key}: ${answer.ok ? "searched" : answer.code}`);
      return "access_denied";
    }
    assert(answer.ok, `${key}: ${answer.code}`);
    const ids = answer.result.records.map((record) => record.id);
    assert(json(ids) === json(t.own_order_ids), `${key}: ids ${json(ids)} vs ORM ${json(t.own_order_ids)}`);
    const all = await call(client, "odoo_search", { model: "sale.order", domain: [], fields: ["id"], limit: 1 });
    assert(all.ok && all.result.count === t.order_ids.length, `${key}: visible count ${all.result?.count} vs ORM ${t.order_ids.length}`);
    return `${ids.length} own, ${all.result.count} visible`;
  });

  await perUser("D4", "Read the other salesperson's order by id: left out unless the user may read it", async (key, client, t) => {
    const other = key === "sp_b" ? facts.a_order_ids[0] : facts.b_order_ids[0];
    const answer = await call(client, "odoo_read", { model: "sale.order", ids: [other], fields: ["name", "amount_total"] });
    if (!t.can_read_orders) {
      assert(!answer.ok && answer.code === "access_denied", `${key}: ${answer.ok ? "read" : answer.code}`);
      return "access_denied";
    }
    assert(answer.ok, `${key}: ${answer.code}`);
    const expected = t.order_ids.includes(other);
    const got = answer.result.records.length === 1;
    assert(got === expected, `${key}: order ${other} returned=${got}, ORM visible=${expected}`);
    return got ? `order ${other} returned (visible to the user)` : `order ${other} left out`;
  });

  const close = (a, b) => Math.abs(a - b) < 0.005;
  await perUser("D5", "Totals by salesperson equal the ORM's for that user and never include hidden orders", async (key, client, t) => {
    const answer = await call(client, "odoo_group", { model: "sale.order", groupby: ["user_id"], aggregates: ["amount_total:sum", "__count"] });
    if (!t.can_read_orders) {
      assert(!answer.ok && answer.code === "access_denied", `${key}: ${answer.ok ? "grouped" : answer.code}`);
      return "access_denied";
    }
    assert(answer.ok, `${key}: ${answer.code}`);
    const got = Object.fromEntries(
      answer.result.groups.map((group) => [group.user_id ? String(group.user_id[0]) : "False", [group["amount_total:sum"], group.__count]]),
    );
    const expected = t.by_user;
    assert(json(Object.keys(got).sort()) === json(Object.keys(expected).sort()), `${key}: groups ${json(got)} vs ${json(expected)}`);
    for (const [salesperson, [sum, count]] of Object.entries(expected)) {
      assert(close(got[salesperson][0], sum) && got[salesperson][1] === count, `${key}: ${salesperson} ${json(got[salesperson])} vs ${json([sum, count])}`);
    }
    const orm = Object.fromEntries(t.orm_group_user.map((group) => [group.user_id ? String(group.user_id[0]) : "False", group["amount_total:sum"]]));
    assert(Object.entries(orm).every(([k, v]) => close(got[k][0], v)), `${key}: differs from formatted_read_group`);
    const total = Object.values(got).reduce((sum, [value]) => sum + value, 0);
    return `${Object.keys(got).length} groups, total ${total.toFixed(2)}`;
  });

  const monthOf = (utc, tz) => {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit" }).formatToParts(new Date(`${utc.replace(" ", "T")}Z`));
    return `${parts.find((p) => p.type === "year").value}-${parts.find((p) => p.type === "month").value}`;
  };
  await perUser("D6", "Totals by month (in the user's time zone) equal the ORM's for that user", async (key, client, t) => {
    const answer = await call(client, "odoo_group", { model: "sale.order", groupby: ["date_order:month"], aggregates: ["amount_total:sum", "__count"], order: "date_order:month asc" });
    if (!t.can_read_orders) {
      assert(!answer.ok && answer.code === "access_denied", `${key}: ${answer.ok ? "grouped" : answer.code}`);
      return "access_denied";
    }
    assert(answer.ok, `${key}: ${answer.code}`);
    const got = Object.fromEntries(
      answer.result.groups.map((group) => [monthOf(group["date_order:month"][0], "Europe/Rome"), [group["amount_total:sum"], group.__count]]),
    );
    const expected = t.by_month;
    assert(json(Object.keys(got).sort()) === json(Object.keys(expected).sort()), `${key}: months ${json(Object.keys(got))} vs ${json(Object.keys(expected))}`);
    for (const [month, [sum, count]] of Object.entries(expected)) {
      assert(close(got[month][0], sum) && got[month][1] === count, `${key}: ${month} ${json(got[month])} vs ORM ${json([sum, count])}`);
    }
    const labels = t.orm_group_month.map((group) => group["date_order:month"][0]).sort();
    assert(json(answer.result.groups.map((g) => g["date_order:month"][0]).sort()) === json(labels), `${key}: buckets differ from formatted_read_group`);
    return `${Object.keys(got).length} months, Sept ${got["2026-09"]?.[1] ?? 0} orders`;
  });

  await perUser("D7", "Hidden fields and models are refused for everyone, administrators included", async (key, client) => {
    const probes = [
      ["odoo_search", { model: "sale.order", fields: ["access_token"], limit: 1 }, ["invalid_params", "access_denied"]],
      ["odoo_search", { model: "res.users", fields: ["password"], limit: 1 }, ["invalid_params"]],
      ["odoo_search", { model: "ir.config_parameter", fields: ["key", "value"], limit: 1 }, ["unknown_model"]],
      ["odoo_describe_model", { model: "res.users.apikeys" }, ["unknown_model"]],
      ["odoo_read", { model: "o3p.mcp.token", ids: [1], fields: ["id"] }, ["unknown_model"]],
      ["odoo_group", { model: "payment.provider", groupby: ["code"], aggregates: ["__count"] }, ["unknown_model"]],
    ];
    for (const [tool, args, codes] of probes) {
      const answer = await call(client, tool, args);
      assert(!answer.ok && codes.includes(answer.code), `${key}: ${tool} ${json(args)} → ${answer.ok ? json(answer.result).slice(0, 80) : answer.code}`);
    }
    return "6/6 refused";
  });

  await perUser("D8", "The generated `odoo` knowledge bundle is searchable next to the other bundles", async (key, client) => {
    const found = ctx.toolJson(await client.callTool({ name: "search_concepts", arguments: { query: "sales order" } }));
    const uris = (found.results || []).map((entry) => entry.uri);
    assert(uris.includes("okf://odoo/models/sale.order"), `${key}: ${uris.slice(0, 3).join(",")}`);
    const concept = JSON.stringify(ctx.toolJson(await client.callTool({ name: "get_concept", arguments: { uri: "okf://odoo/models/sale.order" } })));
    assert(concept.includes("amount_total"), `${key}: concept lacks fields`);
    const shop = ctx.toolJson(await client.callTool({ name: "search_concepts", arguments: { query: "refund" } }));
    assert(shop.results?.some((entry) => entry.uri.startsWith("okf://shop/")), `${key}: shop bundle not found`);
    return "found";
  });

  await check("D9", "The `odoo` bundle carries no API key, database name or server address", async () => {
    const files = listFiles(ctx.state.project.odoo);
    const leaks = [];
    for (const file of files) {
      const text = fs.readFileSync(path.join(ctx.state.project.odoo, file), "utf8");
      for (const needle of [sales.producer_api_key, "o3p_e2e", new URL(ISSUER).host]) {
        if (text.includes(needle)) leaks.push(`${file} contains ${needle === sales.producer_api_key ? "the API key" : needle}`);
      }
    }
    assert(leaks.length === 0, leaks.slice(0, 5).join("; "));
    info("D9", `the mcp_only user, who may not read sale.order, finds its fields and record rules in the knowledge bundle (metadata shared with every MCP user)`);
    return `${files.length} files clean`;
  });

  // -------------------------------------------------------------------------
  // Latency as the restricted salesperson
  // -------------------------------------------------------------------------

  const latencies = {};
  const latencyCalls = [
    ["odoo_find_models", { query: "sale" }],
    ["odoo_describe_model", { model: "sale.order" }],
    ["odoo_search", { model: "sale.order", domain: [["state", "=", "sale"]], fields: ["name", "partner_id", "amount_total", "date_order"], limit: 50 }],
    ["odoo_read", { model: "sale.order", ids: facts.a_order_ids.slice(0, 10), fields: ["name", "partner_id", "amount_total", "state"] }],
    ["odoo_group", { model: "sale.order", groupby: ["date_order:month"], aggregates: ["amount_total:sum", "__count"] }],
  ];
  for (let round = 0; round < 20; round += 1) {
    for (const [tool, args] of latencyCalls) {
      const answer = await call(A, tool, args);
      if (answer.ok) (latencies[tool] ||= []).push(answer.ms);
    }
  }
  const pct = (list, p) => {
    const sorted = [...list].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];
  };
  await check("D10", "Latency per tool call as the restricted salesperson (20 calls each, through okf-mcp to Odoo)", async () => {
    const parts = latencyCalls.map(([tool]) => {
      const list = latencies[tool] || [];
      assert(list.length === 20, `${tool}: ${list.length}/20 succeeded`);
      return `${tool} median ${Math.round(pct(list, 50))} ms p95 ${Math.round(pct(list, 95))} ms`;
    });
    return parts.join("; ");
  });

  // -------------------------------------------------------------------------
  // The hostile salesperson
  // -------------------------------------------------------------------------

  const hidden = facts.hidden_from_a;
  const onlyB = facts.only_b_customers;

  await check("H1", "Reading hidden ids answers exactly like reading ids that do not exist (no existence oracle)", async () => {
    const hiddenAnswer = await call(A, "odoo_read", { model: "sale.order", ids: [hidden[0]], fields: ["name"] });
    const missing = await call(A, "odoo_read", { model: "sale.order", ids: [facts.nonexistent_order_id], fields: ["name"] });
    assert(json(hiddenAnswer.result) === json(missing.result), `hidden ${json(hiddenAnswer.result)} vs missing ${json(missing.result)}`);
    assert(hiddenAnswer.result.records.length === 0, "hidden order returned");
    return json(hiddenAnswer.result);
  });

  await check("H2", "Domains aimed at hidden orders find nothing (ids, B-only customers, B as salesperson, child_of)", async () => {
    const domains = [
      [["id", "in", hidden]],
      [["partner_id", "in", onlyB]],
      [["user_id", "=", U.sp_b.id]],
      [["partner_id.name", "ilike", "Dolomiti"]],
    ];
    for (const domain of domains) {
      const answer = await call(A, "odoo_search", { model: "sale.order", domain, fields: ["id"], limit: 200 });
      assert(answer.ok && answer.result.count === 0, `LEAK: ${repro("odoo_search", { model: "sale.order", domain, fields: ["id"] })} → ${answer.ok ? `count ${answer.result.count}` : answer.code}`);
    }
    const childOf = await call(A, "odoo_search", { model: "sale.order", domain: [["id", "child_of", hidden]], fields: ["id"] });
    assert(!childOf.ok || childOf.result.count === 0, `LEAK: child_of returned ${childOf.result?.count}`);
    return `4 domains → 0; child_of → ${childOf.ok ? 0 : childOf.code}`;
  });

  await check("H3", "Related-path and `any` domains through hidden orders find nothing", async () => {
    const domains = [
      [["sale_order_ids.user_id", "=", U.sp_b.id]],
      [["sale_order_ids", "any", [["user_id", "=", U.sp_b.id]]]],
      [["sale_order_ids.amount_total", ">", 0], ["id", "in", onlyB]],
    ];
    for (const domain of domains) {
      const answer = await call(A, "odoo_search", { model: "res.partner", domain, fields: ["name"], limit: 50 });
      assert(answer.ok && answer.result.count === 0, `LEAK: ${repro("odoo_search", { model: "res.partner", domain, fields: ["name"] })} → ${answer.ok ? answer.result.records.map((r) => r.name).join(",") : answer.code}`);
    }
    return "3 domains → 0";
  });

  await check("H4", "A one-to-many condition with ids (sale_order_ids in <hidden ids>) reveals nothing about hidden orders", async () => {
    const args = { model: "res.partner", domain: [["sale_order_ids", "in", hidden]], fields: ["name"], limit: 50 };
    const answer = await call(A, "odoo_search", args);
    assert(answer.ok, answer.code);
    const leaked = answer.result.records.filter((record) => onlyB.includes(record.id));
    assert(leaked.length === 0, `LEAK: ${repro("odoo_search", args)} → returns ${leaked.map((r) => r.name).join(", ")}, customers that have only orders the user may not read (ORM-as-user record rules are not applied to the last one-to-many hop; one id per call maps each hidden order to its customer)`);
    return "no B-only customer returned";
  });

  await check("H5", "A user without sales rights learns nothing about orders through res.partner.sale_order_ids", async () => {
    const client = sessions.mcp_only.client;
    const args = { model: "res.partner", domain: [["sale_order_ids", "!=", false]], fields: ["name"], limit: 50 };
    const answer = await call(client, "odoo_search", args);
    const one = { model: "res.partner", domain: [["sale_order_ids", "=", hidden[0]]], fields: ["name"] };
    const mapped = await call(client, "odoo_search", one);
    assert(
      (!answer.ok || answer.result.count === 0) && (!mapped.ok || mapped.result.count === 0),
      `LEAK: ${repro("odoo_search", args, "mcp_only")} → ${answer.result?.count} customers with orders; ${json(one.domain)} → ${mapped.result?.records.map((r) => r.name).join(",")} (the user may not read sale.order at all)`,
    );
    return "nothing";
  });

  await check("H6", "Partner fields that summarise orders show only the user's orders", async () => {
    const args = { model: "res.partner", ids: onlyB, fields: ["name", "sale_order_ids", "sale_order_count"] };
    const answer = await call(A, "odoo_read", args);
    assert(answer.ok, answer.code);
    const leaked = answer.result.records.filter((record) => record.sale_order_ids.length || record.sale_order_count);
    assert(leaked.length === 0, `LEAK: ${repro("odoo_read", args)} → ${json(leaked)}`);
    return "sale_order_ids [] and sale_order_count 0 for B-only customers";
  });

  await check("H7", "Order lines and the sales analysis report total only the user's orders", async () => {
    const lines = await call(A, "odoo_search", { model: "sale.order.line", domain: [["order_id", "in", hidden]], fields: ["id"] });
    assert(lines.ok && lines.result.count === 0, `LEAK: order lines of hidden orders: ${lines.result?.count}`);
    const report = await call(A, "odoo_group", { model: "sale.report", groupby: ["user_id"], aggregates: ["price_total:sum", "__count"] });
    assert(report.ok, report.code);
    const orm = truth.sp_a.orm_report_user;
    const got = report.result.groups.map((g) => [g.user_id ? g.user_id[0] : false, g["price_total:sum"]]);
    const expected = orm.map((g) => [g.user_id ? g.user_id[0] : false, g["price_total:sum"]]);
    assert(json(got.sort()) === json(expected.sort()), `LEAK: sale.report ${json(got)} vs ORM-as-user ${json(expected)}`);
    return `sale.report groups ${json(got)}`;
  });

  await check("H8", "Forecast quantities Odoo computes with elevated rights appear exactly as Odoo shows the user (documented)", async () => {
    const args = { model: "product.product", ids: [facts.b_only_product.id], fields: ["name", "outgoing_qty", "virtual_available"] };
    const answer = await call(A, "odoo_read", args);
    assert(answer.ok, answer.code);
    const record = answer.result.records[0];
    assert(record.outgoing_qty === sales.orm_outgoing_qty_as_a, `API ${record.outgoing_qty} vs ORM-as-user ${sales.orm_outgoing_qty_as_a}`);
    info("H8", `${repro("odoo_read", args)} → outgoing_qty ${record.outgoing_qty}, virtual_available ${record.virtual_available} for a product sold only in salesperson B's confirmed orders: the value the product form shows the user (README "Values Odoo computes with elevated rights")`);
    return `outgoing_qty ${record.outgoing_qty} = ORM as the user`;
  });

  for (const pointer of sales.pointer_models) {
    const args = { model: pointer.model, domain: [[pointer.model_field, "=", "sale.order"], [pointer.id_field, "in", hidden]], fields: ["id"], limit: 200 };
    const answer = await call(A, "odoo_search", args);
    if (answer.ok && answer.result.count > 0) {
      const sample = await call(A, "odoo_search", { ...args, fields: [pointer.id_field, ...(pointer.model === "mail.followers" ? ["partner_id"] : [])], limit: 3 });
      await check(`H9:${pointer.model}`, `${pointer.model} reveals nothing about hidden orders`, async () => {
        throw new Error(`LEAK: ${repro("odoo_search", args)} → ${answer.result.count} rows, e.g. ${json(sample.result?.records)} (ids of orders the user may not read and who follows them)`);
      });
    }
  }
  await check("H9", "Models that point at records by (model, id) were swept for traces of hidden orders", async () => {
    return `${sales.pointer_models.length} models swept: ${sales.pointer_models.map((p) => p.model).join(", ")} (failures listed as H9:<model>)`;
  });

  await check("H10", "Saved payment methods: the provider's token reference is not readable", async () => {
    const args = { model: "payment.token", domain: [], fields: ["partner_id", "provider_ref", "payment_details"] };
    const answer = await call(A, "odoo_search", args);
    const exposed = answer.ok && answer.result.records.some((record) => record.provider_ref);
    const admin = await call(sessions.d_admin.client, "odoo_search", args);
    assert(
      !exposed,
      `LEAK: ${repro("odoo_search", args)} → ${json(answer.result?.records)}; the administrator gets ${admin.ok ? "it too" : admin.code}. Odoo lets every salesperson read every payment.token (sale's payment_token_salesman_rule) and provider_ref is the payment provider's reference to the saved card; neither the model nor the field is on the default denylist`,
    );
    return answer.ok ? "provider_ref hidden" : answer.code;
  });

  await check("H11", "Hidden fields cannot be used in domains, paths, orders, groupings or aggregates (salesperson and administrator)", async () => {
    const probes = [
      ["odoo_search", { model: "sale.order", domain: [["access_token", "!=", false]], fields: ["name"] }],
      ["odoo_search", { model: "sale.order", domain: [["user_id.password", "!=", false]], fields: ["name"] }],
      ["odoo_search", { model: "sale.order", domain: [["partner_id.user_ids.api_key_ids.name", "!=", false]], fields: ["name"] }],
      ["odoo_search", { model: "sale.order", fields: ["name"], order: "access_token desc" }],
      ["odoo_group", { model: "sale.order", groupby: ["access_token"], aggregates: ["__count"] }],
      ["odoo_group", { model: "sale.order", groupby: ["state"], aggregates: ["access_token:count_distinct"] }],
      ["odoo_search", { model: "res.partner", domain: [["user_ids.api_key_ids", "!=", false]], fields: ["name"] }],
    ];
    for (const key of ["sp_a", "d_admin"]) {
      for (const [tool, args] of probes) {
        const answer = await call(sessions[key].client, tool, args);
        assert(!answer.ok, `LEAK: ${repro(tool, args, key)} → ${String(json(answer.result)).slice(0, 120)}`);
      }
    }
    return `${probes.length} probes × 2 users refused`;
  });

  await check("H12", "Aggregates, groupings and orders over related paths are refused, also when sent past the tool schema", async () => {
    const tokenA = tokenOf("sp_a");
    const params = [
      ["group", { model: "sale.order", domain: [], groupby: ["state"], aggregates: ["order_line.price_total:sum"] }],
      ["group", { model: "sale.order", domain: [], groupby: ["partner_id.country_id"], aggregates: ["__count"] }],
      ["search", { model: "sale.order", domain: [], fields: ["name"], order: "partner_id.name" }],
      ["search", { model: "res.partner", domain: [["sale_order_ids.order_line.product_id.default_code.x", "=", "x"]], fields: ["name"] }],
    ];
    for (const [op, p] of params) {
      const answer = await direct(op, tokenA, p);
      assert(answer.status === 400 && answer.body.error?.code === "invalid_params", `LEAK: data API ${op} ${json(p)} → ${answer.status} ${json(answer.body).slice(0, 120)}`);
    }
    const viaTool = await call(A, "odoo_group", { model: "sale.order", groupby: ["state"], aggregates: ["order_line.price_total:sum"] });
    assert(!viaTool.ok, "the tool accepted a related aggregate");
    return `4 direct probes → invalid_params; tool → ${viaTool.code}`;
  });

  await check("H13", "Describe on restricted and hidden models", async () => {
    const out = [];
    const cases = [
      ["mcp_only", "sale.order", ["access_denied"]],
      ["sp_a", "ir.config_parameter", ["unknown_model"]],
      ["sp_a", "o3p.mcp.client", ["unknown_model"]],
      ["sp_a", "res.users.apikeys", ["unknown_model"]],
      ["sp_a", "ir.cron", ["access_denied"]],
      ["d_admin", "ir.config_parameter", ["unknown_model"]],
    ];
    for (const [key, model, codes] of cases) {
      const answer = await call(sessions[key].client, "odoo_describe_model", { model });
      assert(!answer.ok && codes.includes(answer.code), `${key} describe ${model} → ${answer.ok ? "described" : answer.code}`);
      out.push(`${key} ${model} ${answer.code}`);
    }
    const users = await call(A, "odoo_describe_model", { model: "res.users" });
    const sensitive = users.result.fields.map((field) => field.name).filter((name) => /password|secret|token|api_key/.test(name));
    assert(sensitive.length === 0, `res.users exposes ${sensitive.join(",")}`);
    return out.join("; ");
  });

  await check("H14", "Oversized requests are refused with bounded codes", async () => {
    const tokenA = tokenOf("sp_a");
    const results = [];
    const viaTool = [
      ["odoo_search", { model: "sale.order", fields: ["name"], limit: 201 }],
      ["odoo_read", { model: "sale.order", ids: Array.from({ length: 101 }, (_, i) => i + 1), fields: ["name"] }],
      ["odoo_search", { model: "sale.order", domain: Array.from({ length: 101 }, () => ["id", ">", 0]), fields: ["name"] }],
    ];
    for (const [tool, args] of viaTool) {
      const answer = await call(A, tool, args);
      assert(!answer.ok && SCHEMA_CODES.has(answer.code), `${tool} oversized → ${answer.ok ? "accepted" : answer.code}`);
      results.push(answer.code);
    }
    const bigIn = await call(A, "odoo_search", { model: "sale.order", domain: [["id", "in", Array.from({ length: 1001 }, (_, i) => i + 1)]], fields: ["name"] });
    const bigString = await call(A, "odoo_search", { model: "sale.order", domain: [["name", "ilike", "x".repeat(1001)]], fields: ["name"] });
    assert(!bigIn.ok && !bigString.ok, "large operands accepted");
    results.push(bigIn.code, bigString.code);
    const body = await ctx.mcpPost(MCP_URL, { token: tokenA, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "odoo_search", arguments: { model: "sale.order", fields: ["name"], domain: [["name", "=", "x".repeat(1_100_000)]] } } }) });
    assert(body.status === 413, `1.1 MB MCP request → ${body.status}`);
    results.push("413");
    const odooBody = await direct("search", tokenA, { model: "sale.order", fields: ["name"], domain: [["name", "=", "x".repeat(300_000)]] });
    results.push(`data API ${odooBody.status}`);
    assert(odooBody.status === 413, `300 KB data API body → ${odooBody.status}`);
    // A small answer limit set by an administrator gives result_too_large.
    const admin = await ctx.adminSession();
    let small;
    try {
      await admin.saveSettings({ o3p_mcp_api_max_response_kb: 1 });
      small = await call(A, "odoo_search", { model: "sale.order", fields: ["name", "partner_id", "amount_total"], limit: 50 });
    } finally {
      await admin.saveSettings({ o3p_mcp_api_max_response_kb: 1000 });
    }
    assert(!small.ok && small.code === "result_too_large", `1 KB limit → ${small.ok ? "accepted" : small.code}`);
    results.push(small.code);
    return results.join(", ");
  });

  await check("H15", "Malformed domains are refused with fixed messages, never Odoo's error text", async () => {
    const tokenA = tokenOf("sp_a");
    const viaTool = [
      { model: "sale.order", domain: "[('id','=',1)]", fields: ["name"] },
      { model: "sale.order", domain: [["id", "inselect", ["select id from res_users", []]]], fields: ["name"] },
      { model: "sale.order", domain: [["a.b.c.d.e", "=", 1]], fields: ["name"] },
    ];
    const codes = [];
    for (const args of viaTool) {
      const answer = await call(A, "odoo_search", args);
      assert(!answer.ok, `accepted ${json(args.domain)}`);
      codes.push(answer.code);
    }
    let nested = ["id", "=", 1];
    for (let depth = 0; depth < 6; depth += 1) nested = ["order_line", "any", [nested]];
    const direct_ = [
      [["id", "=", 1], "&"],
      ["&", ["id", "=", 1]],
      [["id", "=", { $gt: 0 }]],
      [["id", "=", [1, [2]]]],
      [nested],
      [["user_id.partner_id.parent_id.user_id.login", "=", "x"]],
      [["id", "=", 1, 2]],
      [["name", "=like", "%' OR 1=1 --"]],
    ];
    for (const domain of direct_) {
      const answer = await direct("search", tokenA, { model: "sale.order", domain, fields: ["name"] });
      const message = answer.body?.error?.message;
      const harmless = answer.status === 200 && answer.body.result.records.every((r) => truth.sp_a.order_ids.includes(r.id));
      assert(harmless || (answer.status >= 400 && FIXED_MESSAGES.has(message)), `domain ${json(domain)} → ${answer.status} ${json(answer.body).slice(0, 160)}`);
      codes.push(answer.status === 200 ? "200(own rows only)" : answer.body.error.code);
    }
    return codes.join(", ");
  });

  await check("H16", "Tool arguments cannot carry another token, and concurrent users never see each other's orders", async () => {
    const injected = await call(A, "odoo_search", { model: "sale.order", fields: ["name"], token: tokenOf("sp_b") });
    assert(!injected.ok && injected.code === "schema", `token argument → ${injected.ok ? "accepted" : injected.code}`);
    const B = sessions.sp_b.client;
    const args = { model: "sale.order", domain: [], fields: ["user_id"], limit: 200 };
    const answers = await Promise.all(Array.from({ length: 20 }, (_, i) => call(i % 2 ? B : A, "odoo_search", args).then((answer) => [i % 2 ? "sp_b" : "sp_a", answer])));
    for (const [key, answer] of answers) {
      assert(answer.ok, `${key}: ${answer.code}`);
      const foreign = answer.result.records.filter((record) => record.user_id && record.user_id[0] !== U[key].id);
      assert(foreign.length === 0, `LEAK: concurrent ${key} saw ${foreign.length} orders of another salesperson`);
    }
    return "token argument refused by the schema; 20 interleaved calls stayed separate";
  });

  await check("H17", "Without odoo.read the tools are not offered and the data API refuses the token", async () => {
    const result = await ctx.signInAs(U.sp_a.login, { scope: "okf.read" });
    assert(result.outcome === "signed-in" && result.tokens.scope === "okf.read", `granted ${result.tokens?.scope}`);
    const tools = (await result.client.listTools()).tools.map((tool) => tool.name);
    assert(!tools.some((name) => name.startsWith("odoo_")), "odoo tools listed");
    const answer = await call(result.client, "odoo_search", { model: "sale.order", fields: ["name"] });
    assert(!answer.ok, "the call ran");
    const api = await direct("search", result.tokens.access_token, { model: "sale.order", domain: [], fields: ["name"] });
    assert(api.status === 403 && api.body.error.code === "insufficient_scope", `data API → ${api.status} ${json(api.body)}`);
    await result.client.close();
    return `tools absent; call → ${answer.code}; data API → insufficient_scope`;
  });

  await check("H18", "A user calling the data API directly with their own token is refused (resource-server credentials required)", async () => {
    const tokenA = tokenOf("sp_a");
    const none = await direct("search", tokenA, { model: "sale.order", domain: [], fields: ["name"] }, null);
    const client = `Basic ${Buffer.from(`${ctx.sharedClient().client_id}:x`).toString("base64")}`;
    const asClient = await direct("search", tokenA, { model: "sale.order", domain: [], fields: ["name"] }, client);
    const bearer = await direct("search", tokenA, { model: "sale.order", domain: [], fields: ["name"] }, `Bearer ${tokenA}`);
    for (const [label, answer] of [["no credentials", none], ["MCP client id", asClient], ["Bearer token", bearer]]) {
      assert(answer.status === 401 && answer.body.error?.code === "invalid_client", `${label} → ${answer.status}`);
    }
    return "no credentials, a client id, a bearer token → 401 invalid_client";
  });

  await check("H19", "A revoked token stops working at the data API at once, while okf-mcp still caches it for the knowledge tools", async () => {
    const result = await ctx.signInAs(U.sp_a.login);
    const before = await call(result.client, "odoo_search", { model: "sale.order", fields: ["name"], limit: 1 });
    assert(before.ok, before.code);
    const revoked = await oauth.revoke(result.tokens.access_token, result.provider.clientInformation());
    assert(revoked.status === 200, `revoke ${revoked.status}`);
    const after = await call(result.client, "odoo_search", { model: "sale.order", fields: ["name"], limit: 1 });
    const knowledge = await call(result.client, "search_concepts", { query: "refund" });
    await result.client.close();
    assert(!after.ok && after.code === "invalid_token", `after revocation: ${after.ok ? "still answered" : after.code}`);
    return `odoo_search → invalid_token immediately; search_concepts ${knowledge.ok ? "still answers (cache ≤ 60 s)" : `→ ${knowledge.code}`}`;
  });

  await check("H20", "Losing the sales group takes effect at the data API on the next call", async () => {
    const admin = await ctx.adminSession();
    const groupId = sales.groups.salesman;
    try {
      await admin.callKw("res.users", "write", [[U.sp_a.id], { group_ids: [[3, groupId]] }]);
      const answer = await call(A, "odoo_search", { model: "sale.order", fields: ["name"], limit: 1 });
      assert(!answer.ok && answer.code === "access_denied", `after removal: ${answer.ok ? `${answer.result.count} orders` : answer.code}`);
    } finally {
      await admin.callKw("res.users", "write", [[U.sp_a.id], { group_ids: [[4, groupId]] }]);
    }
    const back = await call(A, "odoo_search", { model: "sale.order", fields: ["name"], limit: 1 });
    assert(back.ok, "access not restored");
    return "access_denied right after the removal; restored afterwards";
  });

  await check("H21", "The call log records calls without parameters or results", async () => {
    const admin = await ctx.adminSession();
    const fields = await admin.callKw("o3p.mcp.api.log", "fields_get", [], { attributes: ["type"] });
    const names = Object.keys(fields);
    assert(!names.some((name) => /param|result|domain|token|body/.test(name)), `log fields: ${names.join(",")}`);
    const count = await admin.callKw("o3p.mcp.api.log", "search_count", [[["user_id", "=", U.sp_a.id]]]);
    assert(count > 100, `only ${count} rows for sp_a`);
    return `${count} rows for the salesperson; fields ${names.filter((n) => !n.startsWith("_") && !["id", "display_name", "create_uid", "create_date", "write_uid", "write_date"].includes(n)).join(", ")}`;
  });

  for (const session of Object.values(sessions)) await session.client.close();
}
