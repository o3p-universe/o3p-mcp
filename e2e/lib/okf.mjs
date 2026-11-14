// okf-mcp as a child process (MCP over Streamable HTTP with introspection),
// and raw requests to its endpoint.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

// bin/okf-mcp.js of the okf-mcp checkout: OKF_MCP_DIR, else the package
// this directory depends on by path.
export function okfBin() {
  const dir =
    process.env.OKF_MCP_DIR || path.dirname(require.resolve("@mfdaves/okf-mcp/package.json"));
  return path.join(dir, "bin", "okf-mcp.js");
}

// `root` serves one bundle; `project` an okf.project.yaml (bundles, tool
// packs). `env` adds environment variables (the tool pack's credentials).
export async function startOkf(options) {
  const { root, project, port, publicUrl, issuer, introspection, write = false } = options;
  const args = [okfBin(), ...(project ? ["--project", project] : ["--root", root])];
  if (write) args.push("--write", "--actor", "process:okf-mcp-e2e", "--git-commit");
  args.push(
    "mcp",
    "--http",
    "--port",
    String(port),
    "--public-url",
    publicUrl,
    "--auth",
    `introspect:${issuer}/o3p_mcp/oauth/introspect`,
    "--authorization-server",
    issuer,
    ...(options.extraArgs || []),
  );
  const child = spawn(process.execPath, args, {
    env: {
      ...process.env,
      OKF_INTROSPECTION_CLIENT_ID: introspection.client_id,
      OKF_INTROSPECTION_CLIENT_SECRET: introspection.client_secret,
      ...options.env,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const collect = (chunk) => {
    output += chunk;
  };
  child.stdout.on("data", collect);
  child.stderr.on("data", collect);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`okf-mcp did not start:\n${output}`)), 30000);
    const check = () => {
      if (/listening on/.test(output)) {
        clearTimeout(timer);
        resolve();
      }
    };
    child.stdout.on("data", check);
    child.stderr.on("data", check);
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`okf-mcp exited with ${code}:\n${output}`));
    });
  });
  return {
    child,
    args: args.slice(1),
    output: () => output,
    async stop() {
      if (child.exitCode !== null) return;
      child.kill("SIGTERM");
      await new Promise((resolve) => {
        const timer = setTimeout(() => {
          child.kill("SIGKILL");
          resolve();
        }, 5000);
        child.once("exit", () => {
          clearTimeout(timer);
          resolve();
        });
      });
    },
  };
}

export const INITIALIZE = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-11-25",
    capabilities: {},
    clientInfo: { name: "o3p-mcp-e2e-raw", version: "1.0.0" },
  },
};

// One raw POST to the MCP endpoint. Returns { status, headers, text }.
export async function mcpPost(url, { token, scheme = "Bearer", body = INITIALIZE, headers = {} } = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(token ? { authorization: `${scheme} ${token}` } : {}),
      ...headers,
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: response.status, headers: response.headers, text: await response.text() };
}

// Polls until `refused()` holds; returns the time it held, in ms since
// performance's origin, or throws after `timeoutMs`.
export async function waitFor(refused, { timeoutMs = 90000, intervalMs = 1000 } = {}) {
  const started = performance.now();
  for (;;) {
    if (await refused()) return performance.now();
    if (performance.now() - started > timeoutMs) throw new Error(`still not refused after ${timeoutMs} ms`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
