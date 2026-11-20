// One MCP call as a lab user: what an agent (or a script) uses to act as that
// Odoo user against the lab (lab/up.mjs).
//
//   node e2e/lab/call.mjs --as sp_a --agent a1 --list            # tool names
//   node e2e/lab/call.mjs --as sp_a --agent a1 --schema odoo_sql # one tool in full
//   node e2e/lab/call.mjs --as sp_a --agent a1 odoo_search '{"model":"sale.order","domain":[],"fields":["name"]}'
//
// Prints the tool's answer (long answers are cut on screen). Every call and
// its complete answer is appended to
// e2e/.work/lab/transcripts/<LAB_RUN or "default">/<agent>.<role>.jsonl,
// which is what the oracle (lab/oracle.mjs) judges.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { connectWithToken, signIn } from "../lib/mcp.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const LAB = path.join(WORK, "lab");
const SCREEN_LIMIT = 80_000;

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    as: { type: "string" },
    agent: { type: "string", default: "manual" },
    list: { type: "boolean", default: false },
    schema: { type: "string" },
  },
});
const fail = (message) => {
  console.error(message);
  process.exit(2);
};
const lab = JSON.parse(fs.readFileSync(path.join(LAB, "lab.json"), "utf8"));
const role = values.as;
if (!role || !lab.roles[role]) fail(`--as must be one of: ${Object.keys(lab.roles).join(", ")}`);
if (lab.roles[role].outcome !== "signed-in") fail(`${role} could not sign in (${lab.roles[role].outcome})`);
if (!/^[a-z0-9_-]{1,40}$/i.test(values.agent)) fail("--agent must be a short name");
const [tool, rawArgs] = positionals;
const listing = values.list || Boolean(values.schema);
if (!listing && !tool) fail("give a tool name and its JSON arguments, --list or --schema <tool>");
let args = {};
if (rawArgs) {
  try {
    args = JSON.parse(rawArgs);
  } catch (error) {
    fail(`the arguments are not valid JSON: ${error.message}`);
  }
}

const tokenFile = path.join(LAB, "tokens", `${role}.json`);
const readToken = () => JSON.parse(fs.readFileSync(tokenFile, "utf8")).access_token;

// A new sign-in when the stored token is no longer accepted (it lives an hour).
async function renew() {
  const setup = JSON.parse(fs.readFileSync(path.join(WORK, "setup.json"), "utf8"));
  const result = await signIn({
    mcpUrl: lab.mcpUrl,
    issuer: lab.issuer,
    login: lab.roles[role].login,
    password: setup.password,
    clientInformation: lab.client,
  });
  if (result.outcome !== "signed-in") fail(`${role} could not sign in again (${result.outcome})`);
  fs.writeFileSync(tokenFile, JSON.stringify({ access_token: result.tokens.access_token }), { mode: 0o600 });
  return result.client;
}

async function connected() {
  try {
    return (await connectWithToken(lab.mcpUrl, readToken())).client;
  } catch {
    return renew();
  }
}

const client = await connected();
const started = Date.now();
let answer;
try {
  answer = listing ? await client.listTools() : await client.callTool({ name: tool, arguments: args });
} catch (error) {
  answer = { isError: true, content: [{ type: "text", text: `MCP error: ${error.message}` }] };
}
await client.close().catch(() => {});

// --list: one line per tool; --schema: that tool's description and input schema.
function describeTools(tools) {
  if (values.schema) {
    const found = tools.find((entry) => entry.name === values.schema);
    return found
      ? JSON.stringify({ name: found.name, description: found.description, inputSchema: found.inputSchema }, null, 2)
      : `No tool named ${values.schema} is offered to ${role}.`;
  }
  return tools.map((entry) => `${entry.name}: ${(entry.description || "").split(". ")[0].slice(0, 160)}`).join("\n");
}
const text = answer.tools
  ? describeTools(answer.tools)
  : (answer.content || []).map((block) => (block.type === "text" ? block.text : `[${block.type}]`)).join("\n");

const dir = path.join(LAB, "transcripts", process.env.LAB_RUN || "default");
fs.mkdirSync(dir, { recursive: true });
fs.appendFileSync(
  path.join(dir, `${values.agent}.${role}.jsonl`),
  `${JSON.stringify({
    at: new Date(started).toISOString(),
    ms: Date.now() - started,
    agent: values.agent,
    role,
    tool: listing ? "tools/list" : tool,
    args,
    isError: Boolean(answer.isError),
    answer: text,
  })}\n`,
);

console.log(
  text.length > SCREEN_LIMIT
    ? `${text.slice(0, SCREEN_LIMIT)}\n… (${text.length - SCREEN_LIMIT} more characters not shown; ask for less)`
    : text,
);
