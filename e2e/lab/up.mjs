// The lab: okf-mcp kept running on the end-to-end stack, with one signed-in
// token per test user, so that many clients (scripts or agents) can act as
// different Odoo users at once through lab/call.mjs.
//
//   E2E_KEEP=1 e2e/run.sh --only none     # the stack, kept (e2e/.work)
//   node e2e/lab/up.mjs                    # this; stays in the foreground
//
// Needs OKF_MCP_DIR and OKF_ODOO_DIR as e2e/run.sh does. Writes
// e2e/.work/lab/lab.json and one token file per role, prints "LAB READY" and
// serves until it receives SIGINT or SIGTERM.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { signIn } from "../lib/mcp.mjs";
import { startOkf } from "../lib/okf.mjs";
import { ENV, createProject, runProducer } from "../lib/project.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.resolve(here, "..", ".work");
const LAB = path.join(WORK, "lab");

const setup = JSON.parse(fs.readFileSync(path.join(WORK, "setup.json"), "utf8"));
const sales = JSON.parse(fs.readFileSync(path.join(WORK, "sales.json"), "utf8"));
const port = Number(process.env.E2E_MCP_PORT || 18766);
const mcpUrl = `http://localhost:${port}/mcp`;

fs.rmSync(LAB, { recursive: true, force: true });
fs.mkdirSync(path.join(LAB, "tokens"), { recursive: true });

const project = createProject(path.join(LAB, "project"), { issuer: setup.issuer, database: "o3p_e2e" });
const { receipt } = runProducer(project, sales.producer_api_key);
if (!receipt.applied || !receipt.valid) throw new Error("the okf-odoo producer run failed");

const server = await startOkf({
  issuer: setup.issuer,
  introspection: setup.introspection,
  project: project.file,
  port,
  publicUrl: mcpUrl,
  write: true,
  env: {
    [ENV.clientId]: setup.introspection.client_id,
    [ENV.clientSecret]: setup.introspection.client_secret,
  },
});

// One sign-in per role, through the real OAuth flow; the first registers the
// client the others reuse.
const roles = {};
let client;
for (const [key, user] of Object.entries({ ...setup.users, ...sales.users })) {
  let result;
  try {
    result = await signIn({
      mcpUrl,
      issuer: setup.issuer,
      login: user.login,
      password: setup.password,
      clientInformation: client,
    });
  } catch (error) {
    result = { outcome: `failed: ${error.message}` };
  }
  roles[key] = { login: user.login, name: user.name, outcome: result.outcome };
  if (result.outcome !== "signed-in") continue;
  client ||= result.provider.clientInformation();
  roles[key].scope = result.tokens.scope;
  fs.writeFileSync(
    path.join(LAB, "tokens", `${key}.json`),
    JSON.stringify({ access_token: result.tokens.access_token }),
    { mode: 0o600 },
  );
  await result.client.close();
}

fs.writeFileSync(
  path.join(LAB, "lab.json"),
  JSON.stringify({ mcpUrl, issuer: setup.issuer, client, roles }, null, 2),
);
console.log(
  `LAB READY ${mcpUrl}: ${Object.entries(roles)
    .map(([key, role]) => `${key}=${role.outcome}`)
    .join(" ")}`,
);

const stop = async () => {
  await server.stop();
  process.exit(0);
};
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
server.child.once("exit", (code) => {
  console.error(`okf-mcp exited with ${code}`);
  process.exit(1);
});
setInterval(() => {}, 1 << 30);
