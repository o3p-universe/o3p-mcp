// Record writes across several users, on a running lab whose writes were
// opened (lab.sh up, then lab.sh writes): what each user wrote through MCP is
// read back through Odoo's own web interface as each user, and every sales
// order outside the scenario is compared before and after.
//
//   node e2e/lab/writes.mjs
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
const run = `writes-${stamp}`;
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
    [path.join(here, "call.mjs"), "--as", role, "--agent", "writes", tool, JSON.stringify(args)],
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
    [path.join(here, "call.mjs"), "--as", role, "--agent", "writes", "--list"],
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

// Every sales order as the sales manager sees it, by id.
async function orders() {
  const rows = await searchRead("d_admin", "sale.order", [], ["write_date", "state", "client_order_ref"]);
  return new Map(rows.map((row) => [row.id, `${row.write_date}|${row.state}|${row.client_order_ref}`]));
}
const before = await orders();
const made = {};

await check("Users without MCP records / Editor are offered no writing tool and cannot call one", async () => {
  for (const role of ["mcp_only", "editor"]) {
    const tools = await offered(role);
    assert(!tools.includes("odoo_create") && !tools.includes("odoo_update"), `${role} is offered a writing tool`);
    const answer = await call(role, "odoo_create", {
      model: "res.partner",
      records: [{ name: `Lab refused ${stamp}` }],
    });
    assert(!answer.ok, `${role}: the call ran: ${answer.text.slice(0, 120)}`);
  }
  const found = await searchRead("d_admin", "res.partner", [["name", "=", `Lab refused ${stamp}`]], ["name"]);
  assert(found.length === 0, "a refused call created a contact");
  return "mcp_only and editor: tools absent, call refused, nothing created";
});

await check("A salesperson creates a customer and a quotation with two lines", async () => {
  const context = await call("sp_a", "odoo_context", {});
  assert(context.ok && context.result.user.id === users.sp_a.id, context.text.slice(0, 120));
  const customer = await call("sp_a", "odoo_create", {
    model: "res.partner",
    records: [{ name: `Lab customer ${stamp}`, email: `lab-${stamp}@example.test` }],
    company: context.result.company.id,
  });
  assert(customer.ok, customer.text.slice(0, 200));
  made.partner = customer.result.records[0].id;
  const products = await call("sp_a", "odoo_search", {
    model: "product.product",
    domain: [["sale_ok", "=", true]],
    fields: ["name"],
    limit: 2,
    order: "id",
  });
  assert(products.ok && products.result.records.length === 2, products.text.slice(0, 200));
  const order = await call("sp_a", "odoo_create", { model: "sale.order", records: [{ partner_id: made.partner }] });
  assert(order.ok, order.text.slice(0, 200));
  made.order = order.result.records[0].id;
  const lines = await call("sp_a", "odoo_create", {
    model: "sale.order.line",
    records: products.result.records.map((product, index) => ({
      order_id: made.order,
      product_id: product.id,
      product_uom_qty: index + 1,
    })),
  });
  assert(lines.ok && lines.result.records.length === 2, lines.text.slice(0, 200));
  made.lines = lines.result.records.map((record) => record.id);
  const [row] = await searchRead("sp_a", "sale.order", [["id", "=", made.order]], [
    "user_id",
    "partner_id",
    "state",
    "amount_total",
    "order_line",
  ]);
  assert(row && row.user_id[0] === users.sp_a.id, `salesperson ${row?.user_id}`);
  assert(row.partner_id[0] === made.partner && row.state === "draft", `${row.partner_id} ${row.state}`);
  assert(row.order_line.length === 2 && row.amount_total > 0, `${row.order_line.length} lines, total ${row.amount_total}`);
  return `customer ${made.partner}, quotation ${order.result.records[0].display_name} with 2 lines, total ${row.amount_total}`;
});

await check("Her manager reads the quotation; the other salesperson does not see it, in Odoo or through MCP", async () => {
  assert(made.order, "no quotation was created");
  const domain = [["id", "=", made.order]];
  const manager = await searchRead("manager", "sale.order", domain, ["name"]);
  const other = await searchRead("sp_b", "sale.order", domain, ["name"]);
  const viaMcp = await call("sp_b", "odoo_search", { model: "sale.order", domain, fields: ["name"] });
  assert(manager.length === 1, `the manager finds ${manager.length}`);
  assert(other.length === 0, `Odoo shows it to the other salesperson`);
  assert(viaMcp.ok && viaMcp.result.count === 0, `MCP shows it to the other salesperson: ${viaMcp.text.slice(0, 120)}`);
  return `manager 1, other salesperson 0 (Odoo) and 0 (MCP)`;
});

await check("Each write is in the call log with its records and field names, never values", async () => {
  assert(made.order, "no quotation was created");
  const rows = await searchRead(
    "admin",
    "o3p.mcp.api.log",
    [["op", "=", "create"], ["user_id", "=", users.sp_a.id], ["status", "=", 200]],
    ["model", "record_ids", "field_names", "row_count"],
  );
  const byModel = Object.fromEntries(rows.map((row) => [row.model, row]));
  assert(byModel["res.partner"]?.record_ids === String(made.partner), `contact row: ${JSON.stringify(byModel["res.partner"])}`);
  assert(byModel["sale.order"]?.record_ids === String(made.order), `order row: ${JSON.stringify(byModel["sale.order"])}`);
  assert(byModel["sale.order.line"]?.record_ids === made.lines.join(","), `lines row: ${JSON.stringify(byModel["sale.order.line"])}`);
  assert(byModel["res.partner"].field_names === "email,name", byModel["res.partner"].field_names);
  assert(!JSON.stringify(rows).includes(`lab-${stamp}@example.test`), "a value is in the call log");
  return `${rows.length} rows: ${rows.map((row) => `${row.model} [${row.record_ids}]`).join(", ")}`;
});

await check("She changes her own quotation; another salesperson's answers like a missing one and stays as it was", async () => {
  assert(made.order, "no quotation was created");
  const values = { client_order_ref: `LAB-${stamp}` };
  const own = await call("sp_a", "odoo_update", { model: "sale.order", ids: [made.order], values });
  assert(own.ok, own.text.slice(0, 200));
  const [row] = await searchRead("sp_a", "sale.order", [["id", "=", made.order]], ["client_order_ref"]);
  assert(row.client_order_ref === `LAB-${stamp}`, `reference ${row.client_order_ref}`);
  const foreign = sales.facts.b_order_ids[0];
  const hidden = await call("sp_a", "odoo_update", { model: "sale.order", ids: [foreign], values });
  const missing = await call("sp_a", "odoo_update", {
    model: "sale.order",
    ids: [sales.facts.nonexistent_order_id],
    values,
  });
  assert(!hidden.ok && hidden.text.includes("unknown_record"), hidden.text.slice(0, 200));
  assert(hidden.text === missing.text, "a hidden and a missing record answer differently");
  const mixed = await call("sp_a", "odoo_update", {
    model: "sale.order",
    ids: [made.order, foreign],
    values: { client_order_ref: `MIXED-${stamp}` },
  });
  assert(!mixed.ok, "an update naming a hidden record ran");
  const [again] = await searchRead("sp_a", "sale.order", [["id", "=", made.order]], ["client_order_ref"]);
  assert(again.client_order_ref === `LAB-${stamp}`, "part of a refused update was written");
  return "own quotation changed; hidden and missing ids answer alike; nothing written by the refused calls";
});

await check("A model that is not opened and a status field are refused", async () => {
  assert(made.order, "no quotation was created");
  const tag = await call("sp_a", "odoo_create", {
    model: "res.partner.category",
    records: [{ name: `Lab tag ${stamp}` }],
  });
  assert(!tag.ok && tag.text.includes("model_not_writable"), tag.text.slice(0, 200));
  const state = await call("sp_a", "odoo_update", { model: "sale.order", ids: [made.order], values: { state: "sale" } });
  assert(!state.ok && state.text.includes("not_writable"), state.text.slice(0, 200));
  const tags = await searchRead("d_admin", "res.partner.category", [["name", "=", `Lab tag ${stamp}`]], ["name"]);
  const [row] = await searchRead("d_admin", "sale.order", [["id", "=", made.order]], ["state"]);
  assert(tags.length === 0 && row.state === "draft", `${tags.length} tags, state ${row.state}`);
  return "res.partner.category → model_not_writable; state → not_writable; nothing changed";
});

await check("Two salespeople change the same customer at the same moment: one value holds, never a mix", async () => {
  assert(made.partner, "no customer was created");
  const change = (role, tag) =>
    call(role, "odoo_update", {
      model: "res.partner",
      ids: [made.partner],
      values: { ref: `${tag}-${stamp}`, website: `https://${tag.toLowerCase()}.example.test` },
    });
  const answers = await Promise.all([change("sp_a", "A"), change("sp_b", "B")]);
  const succeeded = answers.filter((answer) => answer.ok).length;
  assert(succeeded >= 1, `neither call succeeded: ${answers.map((answer) => answer.text.slice(0, 80)).join(" | ")}`);
  const [row] = await searchRead("d_admin", "res.partner", [["id", "=", made.partner]], ["ref", "website"]);
  const tag = row.ref?.split("-")[0];
  assert(["A", "B"].includes(tag), `ref ${row.ref}`);
  assert(row.website === `https://${tag.toLowerCase()}.example.test`, `ref ${row.ref} with website ${row.website}`);
  const logged = await searchRead(
    "admin",
    "o3p.mcp.api.log",
    [["op", "=", "update"], ["model", "=", "res.partner"], ["record_ids", "=", String(made.partner)], ["status", "=", 200]],
    ["user_id"],
  );
  assert(logged.length === succeeded, `${succeeded} calls succeeded, ${logged.length} are in the log`);
  return `${succeeded} of 2 calls succeeded; the customer holds ${row.ref} with the matching website; ${logged.length} log rows`;
});

await check("No sales order outside the scenario changed", async () => {
  const after = await orders();
  const changed = [...after].filter(([id, state]) => id !== made.order && before.get(id) !== state).map(([id]) => id);
  const gone = [...before.keys()].filter((id) => !after.has(id));
  assert(changed.length === 0 && gone.length === 0, `changed ${changed.join(",")}; gone ${gone.join(",")}`);
  return `${before.size} orders compared before and after`;
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
