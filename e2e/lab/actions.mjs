// Exposed actions across several users, on a running lab whose actions were
// exposed (lab.sh stack, then lab.sh actions, then lab.sh serve): a button, a
// wizard and a server action on sales orders are run through MCP, and what
// each did is read back through Odoo's own web interface as each user.
//
//   node e2e/lab/actions.mjs
//
// Prints one PASS or FAIL line per check; exits non-zero when one fails.
import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { OdooSession } from "../lib/odoo.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const stamp = process.pid;
const run = `actions-${stamp}`;
const env = { ...process.env, LAB_RUN: run };
const exec = promisify(execFile);
const read = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const setup = read(path.join(WORK, "setup.json"));
const sales = read(path.join(WORK, "sales.json"));
const users = { ...setup.users, ...sales.users };

// One MCP call as `role`: the parsed answer, or the text of a refusal.
async function call(role, tool, args) {
  const { stdout } = await exec(
    process.execPath,
    [path.join(here, "call.mjs"), "--as", role, "--agent", "actions", tool, JSON.stringify(args)],
    { env, maxBuffer: 16 * 1024 * 1024 },
  );
  try {
    return { ok: true, result: JSON.parse(stdout), text: stdout };
  } catch {
    return { ok: false, text: stdout };
  }
}

async function offered(role) {
  const { stdout } = await exec(
    process.execPath,
    [path.join(here, "call.mjs"), "--as", role, "--agent", "actions", "--list"],
    { env, maxBuffer: 16 * 1024 * 1024 },
  );
  return stdout.split("\n").map((line) => line.split(":")[0]);
}

// Odoo's own web interface as `role`: the ORM with that user's rights.
const sessions = {};
async function orm(role) {
  sessions[role] ||= await new OdooSession(setup.issuer).login(users[role].login, setup.password);
  return sessions[role];
}
async function searchRead(role, model, domain, fields) {
  return (await orm(role)).callKw(model, "search_read", [domain], { fields, context: { active_test: false } });
}

let failed = 0;
async function check(name, fn) {
  try {
    const detail = await fn();
    console.log(`PASS  ${name}${detail ? ` — ${detail}` : ""}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL  ${name} — ${error.message}`);
  }
}
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
const names = (answer) => answer.result.actions.map((action) => action.name);
const run_ = (role, action, ids, values) =>
  call(role, "odoo_run_action", { action, ids, ...(values ? { values } : {}) });

// Every sales order as the sales manager sees it, by id.
async function orders() {
  const rows = await searchRead("d_admin", "sale.order", [], ["write_date", "state", "client_order_ref"]);
  return new Map(rows.map((row) => [row.id, `${row.write_date}|${row.state}|${row.client_order_ref}`]));
}
const before = await orders();
const made = {};

// A draft quotation of Sara's with two lines, made through MCP.
async function quotation(label) {
  const customer = await call("sp_a", "odoo_create", {
    model: "res.partner",
    records: [{ name: `Lab ${label} ${stamp}` }],
  });
  assert(customer.ok, customer.text.slice(0, 200));
  const products = await call("sp_a", "odoo_search", {
    model: "product.product",
    domain: [["sale_ok", "=", true]],
    fields: ["name"],
    limit: 2,
    order: "id",
  });
  assert(products.ok && products.result.records.length === 2, products.text.slice(0, 200));
  const order = await call("sp_a", "odoo_create", {
    model: "sale.order",
    records: [{ partner_id: customer.result.records[0].id }],
  });
  assert(order.ok, order.text.slice(0, 200));
  const id = order.result.records[0].id;
  const lines = await call("sp_a", "odoo_create", {
    model: "sale.order.line",
    records: products.result.records.map((product, index) => ({
      order_id: id,
      product_id: product.id,
      product_uom_qty: index + 1,
    })),
  });
  assert(lines.ok && lines.result.records.length === 2, lines.text.slice(0, 200));
  return id;
}
const order = async (role, id) =>
  (await searchRead(role, "sale.order", [["id", "=", id]], ["state", "amount_untaxed", "client_order_ref"]))[0];
const discounts = async (id) =>
  (await searchRead("d_admin", "sale.order.line", [["order_id", "=", id]], ["discount"])).map((line) => line.discount);

await check("Users without MCP records / Editor may list actions but are offered no tool that runs one", async () => {
  for (const role of ["mcp_only", "editor"]) {
    const tools = await offered(role);
    assert(tools.includes("odoo_find_actions"), `${role} is not offered odoo_find_actions`);
    assert(!tools.includes("odoo_run_action"), `${role} is offered odoo_run_action`);
    const listed = await call(role, "odoo_find_actions", {});
    assert(listed.ok && listed.result.actions.length === 0, `${role} is listed actions: ${listed.text.slice(0, 160)}`);
    const answer = await run_(role, "confirm-quotation", [sales.facts.a_order_ids[0]]);
    assert(!answer.ok, `${role}: the call ran: ${answer.text.slice(0, 120)}`);
  }
  return "mcp_only and editor: no action listed, no run tool, the call refused";
});

await check("Each user is listed the actions Odoo lets them run", async () => {
  const sara = await call("sp_a", "odoo_find_actions", {});
  const marta = await call("manager", "odoo_find_actions", {});
  assert(sara.ok && marta.ok, `${sara.text.slice(0, 120)} | ${marta.text.slice(0, 120)}`);
  assert(
    JSON.stringify(names(sara)) === JSON.stringify(["confirm-quotation", "discount-quotation"]),
    `the salesperson is listed ${names(sara).join(", ")}`,
  );
  assert(
    JSON.stringify(names(marta)) === JSON.stringify(["confirm-quotation", "discount-quotation", "mark-checked"]),
    `the manager is listed ${names(marta).join(", ")}`,
  );
  const found = await call("sp_a", "odoo_find_actions", { query: "discount", model: "sale.order" });
  assert(found.ok && names(found).join() === "discount-quotation", found.text.slice(0, 160));
  return `salesperson: ${names(sara).join(", ")}; manager: ${names(marta).join(", ")}`;
});

await check("A salesperson discounts her quotation through the wizard and confirms it with the button", async () => {
  made.order = await quotation("discounted");
  const draft = await order("sp_a", made.order);
  const described = await call("sp_a", "odoo_describe_action", { action: "discount-quotation" });
  assert(described.ok, described.text.slice(0, 200));
  const inputs = described.result.inputs.map((input) => input.name);
  assert(
    described.result.wizard === "sale.order.discount" &&
      described.result.steps.join() === "action_apply_discount" &&
      inputs.includes("discount_type") &&
      inputs.includes("discount_percentage") &&
      !inputs.includes("sale_order_id"),
    described.text.slice(0, 300),
  );
  const discounted = await run_("sp_a", "discount-quotation", [made.order], {
    discount_type: "sol_discount",
    discount_percentage: 0.1,
  });
  assert(discounted.ok && discounted.result.done === true, discounted.text.slice(0, 300));
  assert(discounted.result.form?.discount_type === "sol_discount", `form ${JSON.stringify(discounted.result.form)}`);
  const lines = await discounts(made.order);
  const reduced = await order("sp_a", made.order);
  assert(lines.length === 2 && lines.every((value) => value === 10), `line discounts ${lines.join(", ")}`);
  assert(
    Math.abs(reduced.amount_untaxed - draft.amount_untaxed * 0.9) < 0.01,
    `untaxed ${draft.amount_untaxed} became ${reduced.amount_untaxed}`,
  );
  const confirmed = await run_("sp_a", "confirm-quotation", [made.order]);
  assert(confirmed.ok && confirmed.result.records[0]?.id === made.order, confirmed.text.slice(0, 300));
  const sold = await order("sp_a", made.order);
  assert(sold.state === "sale", `state ${sold.state}`);
  return `10% on both lines (${draft.amount_untaxed} → ${reduced.amount_untaxed} untaxed), then ${confirmed.result.records[0].display_name} confirmed`;
});

await check("Odoo's own refusal of a button comes back in Odoo's words and changes nothing", async () => {
  assert(made.order, "no quotation was confirmed");
  const again = await run_("sp_a", "confirm-quotation", [made.order]);
  assert(!again.ok && again.text.includes("query_error"), again.text.slice(0, 300));
  assert(/not in a state requiring confirmation/i.test(again.text), again.text.slice(0, 300));
  const wrong = await run_("sp_a", "discount-quotation", [made.order], { discount_type: "no_such_type" });
  assert(!wrong.ok && wrong.text.includes("bad_value"), wrong.text.slice(0, 300));
  const foreign = await run_("sp_a", "discount-quotation", [made.order], { sale_order_id: sales.facts.b_order_ids[0] });
  assert(!foreign.ok && foreign.text.includes("not_writable"), foreign.text.slice(0, 300));
  const lines = await discounts(made.order);
  assert(lines.every((value) => value === 10), `line discounts ${lines.join(", ")}`);
  return "confirming twice → Odoo's message; a bad value and the record field as a value → refused";
});

await check("Another salesperson's quotation answers like a missing one and stays as it was", async () => {
  made.second = await quotation("untouched");
  const hidden = await run_("sp_b", "confirm-quotation", [made.second]);
  const missing = await run_("sp_b", "confirm-quotation", [sales.facts.nonexistent_order_id]);
  assert(!hidden.ok && hidden.text.includes("unknown_record"), hidden.text.slice(0, 200));
  assert(
    hidden.text.replaceAll(String(made.second), "N") === missing.text.replaceAll(String(sales.facts.nonexistent_order_id), "N"),
    "a hidden and a missing record answer differently",
  );
  const wizard = await run_("sp_b", "discount-quotation", [made.second], {
    discount_type: "sol_discount",
    discount_percentage: 0.5,
  });
  assert(!wizard.ok && wizard.text.includes("unknown_record"), wizard.text.slice(0, 200));
  const mixed = await run_("sp_b", "confirm-quotation", [sales.facts.b_order_ids[0], made.second]);
  assert(!mixed.ok, "a run naming a hidden record ran");
  const row = await order("d_admin", made.second);
  const lines = await discounts(made.second);
  assert(row.state === "draft" && lines.every((value) => value === 0), `state ${row.state}, discounts ${lines.join(", ")}`);
  return "hidden and missing ids answer alike; the quotation is still a draft without discount";
});

await check("An action for sales managers is unknown to a salesperson and runs for the manager", async () => {
  assert(made.order && made.second, "no quotations were made");
  const refused = await run_("sp_a", "mark-checked", [made.order]);
  const unknown = await run_("sp_a", "no-such-action", [made.order]);
  assert(!refused.ok && refused.text.includes("unknown_action"), refused.text.slice(0, 200));
  assert(refused.text === unknown.text, "an action for others and a missing one answer differently");
  const ran = await run_("manager", "mark-checked", [made.order, made.second]);
  assert(ran.ok && ran.result.records.length === 2, ran.text.slice(0, 300));
  const rows = await Promise.all([order("d_admin", made.order), order("d_admin", made.second)]);
  assert(rows.every((row) => row.client_order_ref === "CHECKED"), rows.map((row) => row.client_order_ref).join(", "));
  return "salesperson: unknown_action, as for a missing name; manager: both orders marked CHECKED";
});

await check("Every run is in the call log with its action, records and input names", async () => {
  assert(made.order && made.second, "no quotations were made");
  const rows = await searchRead(
    "admin",
    "o3p.mcp.api.log",
    [["op", "=", "run_action"], ["user_id", "in", [users.sp_a.id, users.sp_b.id, users.manager.id]]],
    ["action", "record_ids", "field_names", "status", "user_id", "model"],
  );
  const mine = (row) => (row.record_ids || "").split(",").some((id) => [made.order, made.second].includes(Number(id)));
  const done = rows.filter((row) => row.status === 200 && mine(row));
  const summary = done.map((row) => `${row.action}[${row.record_ids}]${row.field_names ? ` ${row.field_names}` : ""}`).sort();
  const expected = [
    `confirm-quotation[${made.order}]`,
    `discount-quotation[${made.order}] discount_percentage,discount_type`,
    `mark-checked[${made.order},${made.second}]`,
  ];
  assert(JSON.stringify(summary) === JSON.stringify(expected), `log: ${summary.join(" | ")}`);
  const refusedRows = rows.filter((row) => row.status !== 200);
  assert(refusedRows.length >= 6, `${refusedRows.length} refused runs recorded`);
  assert(refusedRows.every((row) => !row.record_ids && !row.field_names), "a refused run has records or fields in the log");
  return `${done.length} runs: ${summary.join("; ")}; ${refusedRows.length} refused runs recorded without records`;
});

await check("No sales order outside the scenario changed", async () => {
  const after = await orders();
  const mine = new Set([made.order, made.second]);
  const changed = [...after].filter(([id, state]) => !mine.has(id) && before.get(id) !== state).map(([id]) => id);
  const gone = [...before.keys()].filter((id) => !after.has(id));
  assert(changed.length === 0 && gone.length === 0, `changed ${changed.join(",")}; gone ${gone.join(",")}`);
  return `${before.size} orders compared before and after`;
});

await check("No answer of the run holds a planted canary or a secret of the installation", async () => {
  let verdict;
  try {
    verdict = JSON.parse((await exec(process.execPath, [path.join(here, "oracle.mjs"), "--run", run], { env })).stdout);
  } catch (error) {
    verdict = JSON.parse(error.stdout);
  }
  // This run reads no canary on purpose, so "unproven" says nothing here.
  assert(verdict.leaks.length === 0, `${verdict.verdict}: ${JSON.stringify(verdict.leaks).slice(0, 300)}`);
  return `${verdict.calls} calls, ${verdict.watched.length} canaries and secrets watched, no leak`;
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
