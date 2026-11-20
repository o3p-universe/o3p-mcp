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

import { SERVER_IDENTITY, git } from "../lib/bundle.mjs";
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

// After `lab.sh knowledge`: two restricted bundles next to the open `shop`,
// each in a git repository of its own and seeded with one note that carries
// a canary, and a write policy that lets signed-in users write all three.
// It needs an okf-mcp that knows bundle audiences.
const KNOWLEDGE = fs.existsSync(path.join(WORK, "knowledge.json"));
if (KNOWLEDGE) {
  const stamp = Math.random().toString(16).slice(2, 14);
  const restricted = [
    { id: "sales-notes", audience: "sales", title: "Sales notes", allowed: ["sp_a", "sp_b", "manager", "d_admin"] },
    { id: "management-notes", audience: "management", title: "Management notes", allowed: ["manager", "d_admin"] },
  ];
  const canaries = JSON.parse(fs.readFileSync(path.join(WORK, "canaries.json"), "utf8")).filter(
    (entry) => !entry.id.startsWith("knowledge_"),
  );
  for (const bundle of restricted) {
    const dir = path.join(project.root, bundle.id);
    const canary = `CNRY-KB-${bundle.audience.toUpperCase()}-${stamp}`;
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, "index.md"),
      ["---", 'okf_version: "0.2"', "---", "", `# ${bundle.title}`, "", "- [Seed note](seed.md)", ""].join("\n"),
    );
    fs.writeFileSync(
      path.join(dir, "seed.md"),
      [
        "---",
        'type: "Reference"',
        `title: "Seed note ${canary}"`,
        `description: "What only the ${bundle.audience} audience reads: ${canary}."`,
        `tags: ["seed", "${canary.toLowerCase()}"]`,
        'lang: "en"',
        "generated:",
        '  at: "2026-11-01T09:00:00Z"',
        '  by: "human:seed"',
        'status: "stable"',
        "---",
        "",
        `# Seed note ${canary}`,
        "",
        `The ${bundle.audience} audience keeps its notes here. ${canary}`,
        "",
      ].join("\n"),
    );
    git(dir, "init", "-q", "-b", "main");
    git(dir, "config", "user.name", SERVER_IDENTITY.name);
    git(dir, "config", "user.email", SERVER_IDENTITY.email);
    git(dir, "config", "commit.gpgsign", "false");
    git(dir, "add", "-A");
    git(dir, "commit", "-q", "-m", "Seed the bundle");
    canaries.push({
      id: `knowledge_${bundle.audience}`,
      value: canary,
      where: `the ${bundle.id} knowledge bundle (audience ${bundle.audience})`,
      allowed: bundle.allowed,
    });
  }
  fs.writeFileSync(path.join(WORK, "canaries.json"), JSON.stringify(canaries));
  const config = fs.readFileSync(project.file, "utf8").replace(
    "relationTypes:",
    [
      ...restricted.flatMap((bundle) => [`  - id: ${bundle.id}`, `    root: ${bundle.id}`, `    audiences: [${bundle.audience}]`]),
      "writePolicy:",
      "  defaultEffect: deny",
      "  rules:",
      "    - name: signed-in-users",
      '      actors: ["human:*"]',
      '      bundles: ["shop", "sales-notes", "management-notes"]',
      "relationTypes:",
    ].join("\n"),
  );
  fs.writeFileSync(project.file, config);
}
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
