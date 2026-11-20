// What the agents of writers.workflow.js left in Odoo, read through Odoo's
// own web interface: the customer and quotation one salesperson created,
// whether the other salesperson sees them, the order the other one changed,
// the contact the user without write rights must not have created, and the
// writes in the call log. Compare it with what the agents reported.
//
//   node e2e/lab/writers.readback.mjs <mark>     (the workflow's args.mark)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { OdooSession } from "../lib/odoo.mjs";

const WORK = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", ".work");
const mark = process.argv[2] || "W1";
const setup = JSON.parse(fs.readFileSync(`${WORK}/setup.json`, "utf8"));
const sales = JSON.parse(fs.readFileSync(`${WORK}/sales.json`, "utf8"));
const users = { ...setup.users, ...sales.users };
const session = async (role) => new OdooSession(setup.issuer).login(users[role].login, setup.password);
const admin = await session("d_admin");
const system = await session("admin");
const read = (who, model, domain, fields) =>
  who.callKw(model, "search_read", [domain], { fields, context: { active_test: false } });

const out = {};
const customers = await read(admin, "res.partner", [["name", "=", `Officina Rossi ${mark}`]], ["email", "create_uid"]);
out.customer = customers.map((c) => ({ id: c.id, email: c.email, created_by: c.create_uid[1] }));
const orders = customers.length
  ? await read(admin, "sale.order", [["partner_id", "in", customers.map((c) => c.id)]], ["name", "user_id", "state", "amount_total", "order_line", "create_uid"])
  : [];
out.quotations = [];
for (const order of orders) {
  const lines = await read(admin, "sale.order.line", [["id", "in", order.order_line]], ["product_id", "product_uom_qty", "price_subtotal"]);
  out.quotations.push({
    name: order.name,
    salesperson: order.user_id[1],
    created_by: order.create_uid[1],
    state: order.state,
    total: order.amount_total,
    lines: lines.map((line) => `${line.product_uom_qty} x ${line.product_id[1]}`),
  });
}
// Does the other salesperson see it in Odoo?
const bruno = await session("sp_b");
out.bruno_sees_quotation = orders.length
  ? (await read(bruno, "sale.order", [["id", "in", orders.map((o) => o.id)]], ["name"])).length
  : null;

const brunoOrders = await read(admin, "sale.order", [["user_id", "=", users.sp_b.id]], ["name", "state", "client_order_ref", "write_uid"]);
const confirmed = brunoOrders.filter((o) => o.state === "sale").sort((a, b) => b.id - a.id);
out.bruno = {
  latest_confirmed: confirmed[0] && { name: confirmed[0].name, reference: confirmed[0].client_order_ref, changed_by: confirmed[0].write_uid[1] },
  orders_with_the_new_reference: brunoOrders.filter((o) => o.client_order_ref === `PO-7781-${mark}`).map((o) => o.name),
  other_orders_still_with_their_reference: brunoOrders.filter((o) => (o.client_order_ref || "").startsWith("CNRY-")).length,
  of: brunoOrders.length,
};
out.ditta_verdi_contacts = (await read(admin, "res.partner", [["name", "=", `Ditta Verdi ${mark}`]], ["name"])).length;

const log = await read(system, "o3p.mcp.api.log", [["op", "in", ["create", "update"]]], ["op", "model", "user_id", "status", "error", "record_ids", "field_names"]);
out.write_calls = log.reverse().map((row) => `${row.user_id?.[1] || "?"}: ${row.op} ${row.model || ""} → ${row.status}${row.error ? ` ${row.error}` : ""}${row.record_ids ? ` [${row.record_ids}] (${row.field_names})` : ""}`);
console.log(JSON.stringify(out, null, 2));
