// The verdict of a lab run, from the transcripts lab/call.mjs wrote and the
// canaries lab/plant.py planted. No model takes part in it.
//
//   node e2e/lab/oracle.mjs --run <LAB_RUN> [--canaries <file>]
//
// A leak is a canary, or a secret no user may ever see, inside an answer given
// to a role that is not allowed to see it. A canary nobody allowed has read is
// reported as "unproven": the run then says nothing about it, because the
// string might not be where it was planted. Prints one JSON document; exits 1
// on a leak or an unproven canary.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const LAB = path.join(WORK, "lab");

const { values } = parseArgs({
  options: { run: { type: "string", default: "default" }, canaries: { type: "string" } },
});
const read = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const setup = read(path.join(WORK, "setup.json"));
const sales = read(path.join(WORK, "sales.json"));
const canaries = read(values.canaries || path.join(WORK, "canaries.json"));
// Secrets of the installation itself: nobody may read them through MCP.
const secrets = [
  { id: "introspection_client_secret", value: setup.introspection.client_secret, allowed: [] },
  { id: "producer_api_key", value: sales.producer_api_key, allowed: [] },
  { id: "sql_sandbox_password", value: process.env.O3P_SQL_PASSWORD || "o3p-sql-e2e", allowed: [] },
];
const watched = [...canaries, ...secrets].filter((entry) => entry.value);

const dir = path.join(LAB, "transcripts", values.run);
const records = fs.existsSync(dir)
  ? fs
      .readdirSync(dir)
      .filter((name) => name.endsWith(".jsonl"))
      .flatMap((name) =>
        fs
          .readFileSync(path.join(dir, name), "utf8")
          .split("\n")
          .filter(Boolean)
          .map((line) => JSON.parse(line)),
      )
  : [];

const leaks = [];
const seenByAllowed = new Set();
for (const record of records) {
  for (const entry of watched) {
    if (!record.answer.includes(entry.value)) continue;
    if (entry.allowed.includes(record.role)) {
      seenByAllowed.add(entry.id);
    } else {
      leaks.push({
        canary: entry.id,
        role: record.role,
        agent: record.agent,
        at: record.at,
        tool: record.tool,
        args: record.args,
      });
    }
  }
}
const unproven = canaries
  .filter((entry) => entry.allowed.length && !seenByAllowed.has(entry.id))
  .map((entry) => entry.id);

const perAgent = {};
for (const record of records) {
  const key = `${record.agent} as ${record.role}`;
  perAgent[key] ||= { calls: 0, errors: 0, tools: {} };
  perAgent[key].calls += 1;
  if (record.isError) perAgent[key].errors += 1;
  perAgent[key].tools[record.tool] = (perAgent[key].tools[record.tool] || 0) + 1;
}

const verdict = leaks.length ? "LEAK" : unproven.length ? "UNPROVEN" : "CLEAN";
console.log(
  JSON.stringify(
    { run: values.run, verdict, calls: records.length, watched: watched.map((entry) => entry.id), leaks, unproven, perAgent },
    null,
    2,
  ),
);
process.exit(verdict === "CLEAN" ? 0 : 1);
