// Company knowledge across several users, on a running lab set up with
// `lab.sh knowledge` (two audiences in Odoo, two restricted bundles next to
// the open one): who is offered which bundle, who finds what the others
// wrote, and what happens when a user's groups change.
//
//   node e2e/lab/knowledge.mjs
//
// Prints one PASS or FAIL line per check; exits non-zero when one fails.
import { execFile, execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { OdooSession } from "../lib/odoo.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const PROJECT = path.join(WORK, "lab", "project");
const stamp = process.pid;
const run = `knowledge-${stamp}`;
const env = { ...process.env, LAB_RUN: run };
const exec = promisify(execFile);
const read = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const setup = read(path.join(WORK, "setup.json"));
const sales = read(path.join(WORK, "sales.json"));
const canaries = read(path.join(WORK, "canaries.json"));
const users = { ...setup.users, ...sales.users };
const EVERYONE = ["sp_a", "sp_b", "manager", "d_admin", "editor", "mcp_only"];
const SALES = ["sp_a", "sp_b", "manager", "d_admin"];
const MANAGEMENT = ["manager", "d_admin"];

// One MCP call as `role`: the parsed answer, or the text of a refusal.
async function call(role, tool, args) {
  const { stdout } = await exec(
    process.execPath,
    [path.join(here, "call.mjs"), "--as", role, "--agent", "knowledge", tool, JSON.stringify(args)],
    { env, maxBuffer: 16 * 1024 * 1024 },
  );
  try {
    return { ok: true, result: JSON.parse(stdout), text: stdout };
  } catch {
    return { ok: false, text: stdout };
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

const bundlesOf = async (role) => {
  const answer = await call(role, "list_bundles", {});
  assert(answer.ok, `${role}: ${answer.text.slice(0, 120)}`);
  return (answer.result.bundles || answer.result).map((bundle) => bundle.id).sort();
};
// The titles `role` finds for a query.
const finds = async (role, query) => {
  const answer = await call(role, "search_concepts", { query });
  assert(answer.ok, `${role}: ${answer.text.slice(0, 120)}`);
  return answer.result.results.map((result) => result.title);
};
const write = (role, bundle, file, title, extra = {}) =>
  call(role, "okf_apply_changes", {
    bundle,
    changes: [{ op: "create", path: file, type: "Reference", title, body: `# ${title}\n\nWritten in the lab.`, ...extra }],
  });
// Who of `roles` finds a note with `word` in its title.
async function readers(word, roles = EVERYONE) {
  const found = [];
  for (const role of roles) {
    if ((await finds(role, word)).some((title) => title.includes(word))) found.push(role);
  }
  return found;
}
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

// The canaries planted in Odoo are read once by a user who may, so that the
// oracle can tell at the end that they are where they were planted.
await call("sp_b", "odoo_search", {
  model: "sale.order",
  domain: [["client_order_ref", "like", "CNRY"]],
  fields: ["client_order_ref"],
  limit: 1,
});

await check("Each user is offered the bundles of their audiences", async () => {
  const expected = {
    sp_a: ["odoo", "sales-notes", "shop"],
    sp_b: ["odoo", "sales-notes", "shop"],
    manager: ["management-notes", "odoo", "sales-notes", "shop"],
    d_admin: ["management-notes", "odoo", "sales-notes", "shop"],
    editor: ["odoo", "shop"],
    mcp_only: ["odoo", "shop"],
  };
  for (const [role, bundles] of Object.entries(expected)) {
    const offered = await bundlesOf(role);
    assert(same(offered, bundles), `${role} is offered ${offered.join(", ")}`);
  }
  return "salespeople 3, manager and administrator 4, the others 2";
});

await check("The seeded notes are found by their audiences and by nobody else", async () => {
  for (const canary of canaries.filter((entry) => entry.id.startsWith("knowledge_"))) {
    for (const role of EVERYONE) {
      const titles = await finds(role, "seed note");
      const found = titles.some((title) => title.includes(canary.value));
      assert(found === canary.allowed.includes(role), `${role} ${found ? "finds" : "does not find"} ${canary.id}`);
    }
  }
  return "sales note: 4 readers; management note: 2 readers";
});

const notes = {};
await check("The manager writes a management note: she and the administrator find it, the four others find nothing", async () => {
  notes.board = `Boardnote${stamp}`;
  const answer = await write("manager", "management-notes", `notes/board-${stamp}.md`, `${notes.board} price increase`);
  assert(answer.ok && answer.result.applied === true, answer.text.slice(0, 300));
  const found = await readers(notes.board);
  assert(same(found, MANAGEMENT), `found by ${found.join(", ")}`);
  // By its address the note answers the others like one that does not exist.
  const uri = `okf://management-notes/notes/board-${stamp}`;
  const hidden = await call("sp_a", "get_concept", { uri });
  const missing = await call("sp_a", "get_concept", { uri: `okf://nope/notes/board-${stamp}` });
  assert(hidden.result?.code === "concept_not_found", hidden.text.slice(0, 200));
  assert(hidden.text.replaceAll("management-notes", "nope") === missing.text, hidden.text.slice(0, 200));
  return `found by ${found.join(" and ")}; its address answers the others like a missing one`;
});

await check("A salesperson writes a sales note: the sales audience finds it, the two others do not", async () => {
  notes.sales = `Salesnote${stamp}`;
  const answer = await write("sp_a", "sales-notes", `notes/visit-${stamp}.md`, `${notes.sales} customer visit`);
  assert(answer.ok && answer.result.applied === true, answer.text.slice(0, 300));
  const found = await readers(notes.sales);
  assert(same(found, SALES), `found by ${found.join(", ")}`);
  return `found by ${found.join(", ")}`;
});

await check("A note in the open bundle is found by everyone, and may not point at a restricted note", async () => {
  notes.open = `Opennote${stamp}`;
  const answer = await write("editor", "shop", `notes/canteen-${stamp}.md`, `${notes.open} canteen hours`);
  assert(answer.ok && answer.result.applied === true, answer.text.slice(0, 300));
  const found = await readers(notes.open);
  assert(same(found, EVERYONE), `found by ${found.join(", ")}`);
  notes.pointing = `Pointing${stamp}`;
  const pointing = await write("manager", "shop", `notes/pointer-${stamp}.md`, `${notes.pointing} see the board`, {
    relations: [{ type: "related_to", target: `okf://management-notes/notes/board-${stamp}` }],
  });
  assert(pointing.text.includes("reference_outside_audience"), pointing.text.slice(0, 300));
  assert((await readers(notes.pointing)).length === 0, "the refused note was written");
  return "found by all six; a link into management-notes is refused";
});

await check("A user cannot write into a bundle outside their audiences", async () => {
  const hidden = await write("sp_a", "management-notes", `notes/x-${stamp}.md`, `Intruder${stamp}`);
  const missing = await write("sp_a", "nope", `notes/x-${stamp}.md`, `Intruder${stamp}`);
  const plain = (text) => text.replace(/\d{4}-\d\d-\d\dT[\d:.]+Z/g, "<time>");
  assert(!hidden.result?.applied, "the write was applied");
  assert(plain(hidden.text).replaceAll("management-notes", "nope") === plain(missing.text), hidden.text.slice(0, 200));
  assert((await readers(`Intruder${stamp}`)).length === 0, "the note exists");
  // A user without the knowledge editor group has no write tool at all.
  const reader = await write("sp_b", "sales-notes", `notes/y-${stamp}.md`, `Intruder${stamp}`);
  assert(!reader.result?.applied, "a reader wrote");
  return "management-notes answers the salesperson like a bundle that does not exist";
});

await check("Two users writing at the same moment both succeed, each commit by its author", async () => {
  const [first, second] = await Promise.all([
    write("sp_a", "sales-notes", `notes/a-${stamp}.md`, `Together${stamp} sales`),
    write("manager", "management-notes", `notes/b-${stamp}.md`, `Together${stamp} board`),
  ]);
  assert(first.result?.applied && second.result?.applied, `${first.text.slice(0, 120)} | ${second.text.slice(0, 120)}`);
  const author = (bundle) =>
    execFileSync("git", ["-C", path.join(PROJECT, bundle), "log", "-1", "--format=%an"], { encoding: "utf8" }).trim();
  const authors = [author("sales-notes"), author("management-notes")];
  assert(same(authors, [users.sp_a.name, users.manager.name]), `authors ${authors.join(", ")}`);
  const clean = ["sales-notes", "management-notes", "shop"].every(
    (bundle) => execFileSync("git", ["-C", path.join(PROJECT, bundle), "status", "--porcelain"], { encoding: "utf8" }) === "",
  );
  assert(clean, "a worktree is not clean");
  return `commits by ${authors.join(" and ")}`;
});

await check("A user who leaves the group of an audience loses its bundle within a minute", async () => {
  const admin = await new OdooSession(setup.issuer).login(users.admin.login, setup.password);
  const group = (command) =>
    admin.callKw("res.users", "write", [[users.sp_a.id], { group_ids: [[command, sales.groups.salesman]] }]);
  assert((await bundlesOf("sp_a")).includes("sales-notes"), "the bundle is not offered to begin with");
  await group(3);
  const started = Date.now();
  let gone = false;
  try {
    while (Date.now() - started < 75000) {
      if (!(await bundlesOf("sp_a")).includes("sales-notes")) {
        gone = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
    assert(gone, "the bundle is still offered after 75 s");
    assert(!(await readers(notes.sales, ["sp_a"])).length, "she still finds the sales note");
  } finally {
    await group(4);
  }
  const seconds = Math.round((Date.now() - started) / 1000);
  // The group is back; so is the bundle, again within a minute.
  const restored = Date.now();
  while (!(await bundlesOf("sp_a")).includes("sales-notes")) {
    assert(Date.now() - restored < 75000, "the bundle did not come back after the group was restored");
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  return `gone after ${seconds} s, back ${Math.round((Date.now() - restored) / 1000)} s after the group was restored`;
});

await check("The oracle finds the run clean", async () => {
  let verdict;
  try {
    verdict = JSON.parse((await exec(process.execPath, [path.join(here, "oracle.mjs"), "--run", run], { env })).stdout);
  } catch (error) {
    verdict = JSON.parse(error.stdout);
  }
  assert(verdict.verdict === "CLEAN", `${verdict.verdict}: ${JSON.stringify(verdict.leaks).slice(0, 300)} ${verdict.unproven}`);
  return `${verdict.calls} calls, ${verdict.watched.length} canaries and secrets watched`;
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
