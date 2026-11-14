// The okf project okf-mcp serves in the end-to-end run: the small `shop`
// bundle in its own git repository, the `odoo` bundle the okf-odoo producer
// generates, and the okf-odoo tool pack, installed under the project's
// node_modules as okf-mcp requires (a link to this directory's dependency).
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

import { createBundle } from "./bundle.mjs";
import { okfBin } from "./okf.mjs";

const require = createRequire(import.meta.url);
const ODOO_PACKAGE = "@mfdaves/okf-odoo";

// Environment variables the project config names; values come from setup.
export const ENV = {
  apiKey: "E2E_ODOO_API_KEY",
  clientId: "E2E_O3P_MCP_CLIENT_ID",
  clientSecret: "E2E_O3P_MCP_CLIENT_SECRET",
};

export function createProject(root, { issuer, database }) {
  fs.mkdirSync(path.join(root, "odoo"), { recursive: true });
  const shop = createBundle(path.join(root, "shop"));
  const packageDir = fs.realpathSync(path.dirname(require.resolve(`${ODOO_PACKAGE}/package.json`)));
  const link = path.join(root, "node_modules", ...ODOO_PACKAGE.split("/"));
  fs.mkdirSync(path.dirname(link), { recursive: true });
  fs.symlinkSync(packageDir, link, "dir");
  const config = [
    "project: o3p-mcp e2e",
    "bundles:",
    "  - id: shop",
    "    root: shop",
    "  - id: odoo",
    "    root: odoo",
    "relationTypes: [defined_in, extended_by, references, implies, opens]",
    "producers:",
    "  - name: odoo",
    "    type: odoo",
    `    package: "${ODOO_PACKAGE}"`,
    "    bundle: odoo",
    "    config:",
    `      url: ${issuer}`,
    `      database: ${database}`,
    `      apiKeyEnv: ${ENV.apiKey}`,
    "      source: e2e-odoo",
    "      modules:",
    '        include: ["sale", "sales_team", "o3p_mcp"]',
    "toolPacks:",
    `  - package: "${ODOO_PACKAGE}"`,
    "    config:",
    `      url: ${issuer}`,
    `      clientIdEnv: ${ENV.clientId}`,
    `      clientSecretEnv: ${ENV.clientSecret}`,
    "",
  ];
  const file = path.join(root, "okf.project.yaml");
  fs.writeFileSync(file, config.join("\n"));
  return { root, file, shop, odoo: path.join(root, "odoo") };
}

// Runs the okf-odoo producer once (`okf producer run odoo`); returns the
// receipt and the time it took.
export function runProducer(project, apiKey) {
  const started = performance.now();
  const output = execFileSync(
    process.execPath,
    [okfBin(), "--project", project.file, "producer", "run", "odoo", "--write", "--actor", "process:odoo-refresh"],
    { encoding: "utf8", env: { ...process.env, [ENV.apiKey]: apiKey }, maxBuffer: 64 * 1024 * 1024 },
  );
  return { receipt: JSON.parse(output), ms: performance.now() - started };
}

// Every file of a directory tree, relative paths.
export function listFiles(dir, base = dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? listFiles(full, base) : [path.relative(base, full)];
  });
}
