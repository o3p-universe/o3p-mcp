// The lab's own check, on a running lab (lab.sh up): the users' rights show
// through lab/call.mjs, several users can call at once, and the oracle tells
// a clean run from a leak.
//
//   node e2e/lab/check.mjs
//
// Prints one PASS or FAIL line per check; exits non-zero when one fails.
import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const run = `check-${process.pid}`;
const env = { ...process.env, LAB_RUN: run };
const exec = promisify(execFile);
const read = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const setup = read(path.join(WORK, "setup.json"));
const sales = read(path.join(WORK, "sales.json"));
const canaries = read(path.join(WORK, "canaries.json"));
const orderCanary = canaries.find((entry) => entry.id === "order_ref_of_other_salesperson").value;

// One MCP call as `role`; the answer as text.
async function call(role, tool, args) {
  const { stdout } = await exec(
    process.execPath,
    [path.join(here, "call.mjs"), "--as", role, "--agent", "check", tool, JSON.stringify(args)],
    { env, maxBuffer: 16 * 1024 * 1024 },
  );
  return stdout;
}

// The names of the tools offered to `role`.
async function offered(role) {
  const { stdout } = await exec(
    process.execPath,
    [path.join(here, "call.mjs"), "--as", role, "--agent", "check", "--list"],
    { env, maxBuffer: 16 * 1024 * 1024 },
  );
  return stdout.split("\n").map((line) => line.split(":")[0]);
}

// The oracle's verdict for this run, with the canaries of `file` if given.
async function oracle(file) {
  const args = [path.join(here, "oracle.mjs"), "--run", run, ...(file ? ["--canaries", file] : [])];
  try {
    return JSON.parse((await exec(process.execPath, args, { env })).stdout);
  } catch (error) {
    return JSON.parse(error.stdout);
  }
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

const BY_REFERENCE = {
  model: "sale.order",
  domain: [["client_order_ref", "like", "CNRY"]],
  fields: ["client_order_ref"],
  limit: 1,
};

await check("The salesperson who owns the orders reads their canary", async () => {
  const answer = await call("sp_b", "odoo_search", BY_REFERENCE);
  assert(answer.includes(orderCanary), "the canary is not in the answer");
  return `${JSON.parse(answer).count} orders`;
});

await check("Another salesperson gets no record for the same search", async () => {
  const answer = await call("sp_a", "odoo_search", BY_REFERENCE);
  assert(!answer.includes(orderCanary), "the canary is in the answer");
  assert(JSON.parse(answer).count === 0, `count ${JSON.parse(answer).count}`);
});

await check("A hidden model is unknown to a signed-in user", async () => {
  const answer = await call("sp_a", "odoo_search", { model: "ir.config_parameter", domain: [], fields: ["key", "value"] });
  assert(answer.includes("unknown_model"), answer.slice(0, 120));
});

await check("Six users calling at once each get their own rights", async () => {
  const roles = ["sp_a", "sp_b", "manager", "d_admin", "mcp_only", "editor"];
  const answers = await Promise.all(
    roles.map((role) => call(role, "odoo_search", { model: "sale.order", domain: [], fields: ["name"], limit: 1 })),
  );
  const seen = roles.map((role, index) => {
    const truth = sales.truth[role];
    if (truth?.can_read_orders) {
      const count = JSON.parse(answers[index]).count;
      assert(count === truth.order_ids.length, `${role}: ${count} orders, the ORM gives ${truth.order_ids.length}`);
      return `${role} ${count}`;
    }
    assert(answers[index].includes("access_denied"), `${role}: ${answers[index].slice(0, 80)}`);
    return `${role} refused`;
  });
  return seen.join(", ");
});

// The checks below need the tool pack that offers odoo_context (okf-odoo
// newer than 0.1.2) and o3p_mcp 1.42 or newer.
if ((await offered("sp_a")).includes("odoo_context")) {
  await check("Each user's context names that user, a time zone and the user's companies", async () => {
    const roles = ["sp_a", "sp_b", "manager", "editor"];
    const answers = await Promise.all(roles.map((role) => call(role, "odoo_context", {})));
    return roles
      .map((role, index) => {
        const context = JSON.parse(answers[index]);
        const user = sales.users[role] || setup.users[role];
        assert(context.user.id === user.id && context.user.login === user.login, `${role}: ${answers[index].slice(0, 120)}`);
        assert(context.companies.some((company) => company.id === context.company.id), `${role}: company not among the companies`);
        assert(/^\d{4}-\d{2}-\d{2}$/.test(context.today) && context.tz, `${role}: ${context.today} ${context.tz}`);
        return `${role} ${context.user.name} (${context.tz})`;
      })
      .join(", ");
  });

  await check("Totals without a groupby equal the ORM's for each user", async () => {
    const roles = ["sp_a", "sp_b", "manager"];
    const answers = await Promise.all(
      roles.map((role) => call(role, "odoo_group", { model: "sale.order", aggregates: ["__count", "amount_total:sum"] })),
    );
    return roles
      .map((role, index) => {
        const truth = sales.truth[role];
        const total = Object.values(truth.by_user).reduce((sum, [amount]) => sum + amount, 0);
        const { groups } = JSON.parse(answers[index]);
        assert(groups.length === 1, `${role}: ${groups.length} groups`);
        assert(groups[0].__count === truth.order_ids.length, `${role}: ${groups[0].__count} orders, the ORM gives ${truth.order_ids.length}`);
        assert(Math.abs(groups[0]["amount_total:sum"] - total) < 0.01, `${role}: ${groups[0]["amount_total:sum"]}, the ORM gives ${total}`);
        return `${role} ${groups[0].__count} orders ${groups[0]["amount_total:sum"]}`;
      })
      .join(", ");
  });
} else {
  console.log("SKIP  context and totals: this tool pack does not offer odoo_context");
}

await check("The oracle finds the run clean", async () => {
  const verdict = await oracle();
  assert(verdict.verdict === "CLEAN", `${verdict.verdict}: ${JSON.stringify(verdict.leaks).slice(0, 200)}`);
  return `${verdict.calls} calls`;
});

await check("The oracle reports a leak when the canary is forbidden to its reader", async () => {
  const file = path.join(os.tmpdir(), `o3p-lab-canaries-${process.pid}.json`);
  fs.writeFileSync(file, JSON.stringify(canaries.map((entry) => ({ ...entry, allowed: [] }))));
  try {
    const verdict = await oracle(file);
    assert(verdict.verdict === "LEAK", verdict.verdict);
    assert(
      verdict.leaks.some((leak) => leak.role === "sp_b" && leak.canary === "order_ref_of_other_salesperson"),
      "the leak does not name the reader",
    );
  } finally {
    fs.rmSync(file, { force: true });
  }
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
