// End-to-end suite: o3p_mcp (Odoo 20) as the authorization server and
// okf-mcp over Streamable HTTP as the resource server, driven the way MCP
// clients drive them. Started by run.sh, which brings up the Odoo stack and
// writes the setup file; see run.sh for the environment.
//
//   node suite.mjs --setup <setup.json> --work <directory> [--only 1,2,9]
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { parseArgs } from "node:util";

import { Browser, parseForms, visibleText } from "./lib/browser.mjs";
import { runD4 } from "./d4.mjs";
import { runE4 } from "./e4.mjs";
import { SERVER_IDENTITY, createBundle, git } from "./lib/bundle.mjs";
import { connectWithToken, signIn, toolJson, toolNames } from "./lib/mcp.mjs";
import { mcpPost, startOkf, waitFor } from "./lib/okf.mjs";
import { OAuth, OdooSession, pkce, randomToken } from "./lib/odoo.mjs";
import { ENV, createProject, runProducer } from "./lib/project.mjs";

const { values: argv } = parseArgs({
  options: {
    setup: { type: "string" },
    sales: { type: "string" },
    work: { type: "string" },
    only: { type: "string" },
  },
});
const setup = JSON.parse(fs.readFileSync(argv.setup, "utf8"));
const sales = argv.sales ? JSON.parse(fs.readFileSync(argv.sales, "utf8")) : null;
const only = argv.only ? new Set(argv.only.split(",")) : null;
const ISSUER = setup.issuer;
const MCP_PORT = Number(process.env.E2E_MCP_PORT || 18766);
const MCP2_PORT = Number(process.env.E2E_MCP2_PORT || 18767);
const MCP_URL = `http://localhost:${MCP_PORT}/mcp`;
const MCP2_URL = `http://localhost:${MCP2_PORT}/mcp`;
const MCP_ORIGIN = new URL(MCP_URL).origin;
const PASSWORD = setup.password;
const USERS = setup.users;
const WRITE_TOOLS = ["okf_apply_changes", "okf_validate_changes", "okf_run_producer", "okf_list_proposals", "okf_get_proposal"];
const CACHE_LIMIT_MS = 60_000;
const oauth = new OAuth(ISSUER, setup.introspection);

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------

const results = [];
const seconds = (ms) => `${(ms / 1000).toFixed(1)} s`;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function info(id, text) {
  console.log(`INFO  ${id.padEnd(4)} ${text}`);
}

async function check(id, name, fn) {
  try {
    const detail = await fn();
    results.push({ id, name, ok: true, detail });
    console.log(`PASS  ${id.padEnd(4)} ${name}${detail ? ` — ${detail}` : ""}`);
  } catch (error) {
    results.push({ id, name, ok: false, detail: error.message });
    console.log(`FAIL  ${id.padEnd(4)} ${name} — ${error.message}`);
  }
}

const wanted = (scenario) => !only || only.has(String(scenario));

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

let sharedClient; // the public client the first SDK sign-in registered

const signInTimes = [];

// signIn, recording the duration of every complete sign-in.
async function timedSignIn(options) {
  const result = await signIn(options);
  if (result.outcome === "signed-in") signInTimes.push(result.timings);
  return result;
}

function signInAs(key, options = {}) {
  return timedSignIn({
    mcpUrl: MCP_URL,
    issuer: ISSUER,
    login: USERS[key].login,
    password: PASSWORD,
    clientInformation: options.register ? undefined : sharedClient,
    ...options,
  });
}

async function mcpStatus(token, url = MCP_URL) {
  return (await mcpPost(url, { token })).status;
}

// Polls okf-mcp with `token` until it answers 401; returns the time.
function waitRefused(token, url = MCP_URL) {
  return waitFor(async () => (await mcpStatus(token, url)) === 401, { timeoutMs: 90_000 });
}

async function adminSession() {
  return new OdooSession(ISSUER).login(USERS.admin.login, PASSWORD);
}

async function countFor(admin, model, userId) {
  return admin.callKw(model, "search_count", [[["user_id", "=", userId]]]);
}

function hasOutsideRedirect(browser) {
  return browser.trace.some(
    (entry) => entry.location && new URL(entry.location, ISSUER).origin !== new URL(ISSUER).origin,
  );
}

async function preflight(url, origin, method, headers = []) {
  const response = await fetch(url, {
    method: "OPTIONS",
    headers: {
      origin,
      "access-control-request-method": method,
      ...(headers.length ? { "access-control-request-headers": headers.join(",") } : {}),
    },
  });
  const allowOrigin = response.headers.get("access-control-allow-origin");
  const allowHeaders = (response.headers.get("access-control-allow-headers") || "")
    .toLowerCase()
    .split(/\s*,\s*/)
    .filter(Boolean);
  const allowMethods = (response.headers.get("access-control-allow-methods") || "")
    .toUpperCase()
    .split(/\s*,\s*/);
  const headerAllowed = (header) =>
    allowHeaders.includes(header.toLowerCase()) ||
    (allowHeaders.includes("*") && header.toLowerCase() !== "authorization");
  const ok =
    response.status >= 200 &&
    response.status < 300 &&
    (allowOrigin === "*" || allowOrigin === origin) &&
    headers.every(headerAllowed) &&
    (["GET", "HEAD", "POST"].includes(method) || allowMethods.includes(method));
  return { ok, status: response.status, allowOrigin, allowHeaders: allowHeaders.join(",") };
}

// ---------------------------------------------------------------------------
// C8: the flow every interactive MCP client runs
// ---------------------------------------------------------------------------

async function flowCheck() {
  await check("C8", "MCP client flow: 401 → resource metadata → AS metadata → registration → login → consent → token → MCP", async () => {
    const result = await signInAs("editor", { register: true });
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    sharedClient = result.provider.clientInformation();
    const trace = result.trace;
    const step = (predicate, label, from = 0) => {
      const index = trace.findIndex((entry, position) => position >= from && predicate(entry));
      assert(index >= 0, `missing step: ${label} (trace: ${trace.map((e) => `${e.method} ${e.url} ${e.status}`).join("; ")})`);
      return index;
    };
    const first = step((e) => e.method === "POST" && e.url === MCP_URL && e.status === 401 && !e.authorization, "unauthenticated POST /mcp → 401");
    const challenge = trace[first].wwwAuthenticate || "";
    const resourceMetadata = challenge.match(/resource_metadata="([^"]+)"/)?.[1];
    assert(resourceMetadata === `${MCP_ORIGIN}/.well-known/oauth-protected-resource/mcp`, `resource_metadata is ${resourceMetadata}`);
    const prm = step((e) => e.url === resourceMetadata && e.status === 200, "GET protected-resource metadata", first);
    const asm = step((e) => e.url === `${ISSUER}/.well-known/oauth-authorization-server` && e.status === 200, "GET AS metadata", prm);
    const reg = step((e) => e.url === `${ISSUER}/o3p_mcp/oauth/register` && e.status === 201, "POST register → 201", asm);
    const tok = step((e) => e.url === `${ISSUER}/o3p_mcp/oauth/token` && e.status === 200, "POST token → 200", reg);
    step((e) => e.url === MCP_URL && e.authorization === "Bearer" && e.status === 200, "POST /mcp with Bearer → 200", tok);

    const prmBody = await (await fetch(resourceMetadata)).json();
    assert(prmBody.resource === MCP_URL, `PRM resource ${prmBody.resource}`);
    assert(prmBody.authorization_servers?.includes(ISSUER), `PRM authorization_servers ${prmBody.authorization_servers}`);
    const asBody = await oauth.metadata();
    assert(asBody.issuer === ISSUER && asBody.code_challenge_methods_supported.includes("S256"), "AS metadata issuer/S256");

    // The user agent: authorize → /web/login → authorize → consent → client.
    const ua = result.browser.trace.map((e) => `${e.method} ${new URL(e.url).pathname} ${e.status}`);
    const expected = [
      "GET /o3p_mcp/oauth/authorize 303",
      "GET /web/login 200",
      "POST /web/login 303",
      "GET /o3p_mcp/oauth/authorize 200",
      "POST /o3p_mcp/oauth/authorize 303",
    ];
    assert(JSON.stringify(ua) === JSON.stringify(expected), `user agent went ${ua.join(" → ")}`);
    const consent = result.authorization.consent;
    const text = visibleText(consent.html);
    for (const needle of [USERS.editor.name, MCP_URL, "okf.read", "127.0.0.1:18999"]) {
      assert(text.includes(needle), `consent page does not name ${needle}`);
    }
    assert(consent.headers.get("x-frame-options") === "DENY", "consent page can be framed");
    assert(consent.form.inputs.some(([name, value]) => name === "csrf_token" && value), "consent form has no CSRF token");
    const callback = result.callback;
    assert(callback.searchParams.get("iss") === ISSUER, `iss ${callback.searchParams.get("iss")}`);

    const client = result.client;
    const tools = toolNames(await client.listTools());
    assert(tools.includes("search_concepts") && tools.includes("get_concept"), "read tools missing");
    const found = toolJson(await client.callTool({ name: "search_concepts", arguments: { query: "refund" } }));
    const hit = found.results?.find((entry) => entry.uri.endsWith("/policies/refund-policy"));
    assert(hit, `search_concepts did not find the refund policy: ${JSON.stringify(found).slice(0, 200)}`);
    const concept = toolJson(await client.callTool({ name: "get_concept", arguments: { uri: hit.uri } }));
    const serialized = JSON.stringify(concept);
    assert(serialized.includes("Refund policy") && serialized.includes("500 EUR"), "get_concept did not return the concept");
    await client.close();
    const t = result.timings;
    return `sign-in ${Math.round(t.totalMs)} ms (discovery+registration ${Math.round(t.discoveryMs)}, login ${Math.round(t.browser.loginMs)}, consent ${Math.round(t.browser.decisionMs)}, exchange ${Math.round(t.exchangeMs)}, connect ${Math.round(t.connectMs)}); granted "${result.tokens.scope}"`;
  });
}

// ---------------------------------------------------------------------------
// 1. MCP user: reads only
// ---------------------------------------------------------------------------

const state = {};

async function scenario1() {
  const result = await signInAs("user", { scope: "okf.read okf.write" });
  state.user = result;
  await check("1a", "MCP user asking for okf.read okf.write is granted okf.read only, and reads work", async () => {
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    assert(result.tokens.scope === "okf.read", `granted ${result.tokens.scope}`);
    const consentText = visibleText(result.authorization.consent.html);
    assert(consentText.includes("okf.read") && !consentText.includes("okf.write"), "consent page lists okf.write");
    const found = toolJson(await result.client.callTool({ name: "search_concepts", arguments: { query: "backlog" } }));
    assert(found.results?.some((entry) => entry.uri.endsWith("/runbooks/order-backlog")), "search failed");
    const concept = toolJson(await result.client.callTool({ name: "get_concept", arguments: { uri: found.results[0].uri } }));
    assert(JSON.stringify(concept).includes("200 orders"), "get_concept failed");
    return "search_concepts and get_concept answer";
  });
  await check("1b", "MCP user: write tools absent from tools/list", async () => {
    const tools = toolNames(await result.client.listTools());
    const present = WRITE_TOOLS.filter((name) => tools.includes(name));
    assert(present.length === 0, `present: ${present.join(", ")}`);
    return `${tools.length} tools, none of ${WRITE_TOOLS.join("/")}`;
  });
  await check("1c", "MCP user: calling okf_apply_changes is refused and writes nothing", async () => {
    const head = git(state.bundle, "rev-parse", "HEAD");
    let refusal;
    try {
      const answer = await result.client.callTool({
        name: "okf_apply_changes",
        arguments: { message: "should not land", changes: [{ op: "create", type: "Note", title: "Sneaky", body: "# Sneaky\n" }] },
      });
      assert(answer.isError, `the call succeeded: ${JSON.stringify(answer).slice(0, 200)}`);
      refusal = `tool error: ${answer.content?.[0]?.text?.slice(0, 80)}`;
    } catch (error) {
      refusal = `JSON-RPC error: ${error.message.slice(0, 80)}`;
    }
    assert(git(state.bundle, "rev-parse", "HEAD") === head, "HEAD moved");
    return refusal;
  });
}

// ---------------------------------------------------------------------------
// 2. Editor writes; commit author is the Odoo user
// ---------------------------------------------------------------------------

async function scenario2() {
  await check("2", "Editor: okf_apply_changes creates a concept; author = Odoo user, committer = server, generated.by = human:<login>", async () => {
    const result = await signInAs("editor", { scope: "okf.read okf.write" });
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    assert(result.tokens.scope === "okf.read okf.write", `granted ${result.tokens.scope}`);
    const tools = toolNames(await result.client.listTools());
    assert(tools.includes("okf_apply_changes"), "okf_apply_changes not listed");
    const answer = toolJson(
      await result.client.callTool({
        name: "okf_apply_changes",
        arguments: {
          bundle: "shop",
          message: "docs: add the returns desk",
          changes: [
            {
              op: "create",
              type: "Team",
              title: "Returns desk",
              description: "Handles returned parcels.",
              body: "# Returns desk\n\nHandles returned parcels for the order service.\n",
              tags: ["orders", "team"],
              relations: [{ type: "related_to", target: "/services/order-service.md" }],
            },
          ],
        },
      }),
    );
    assert(answer.status === "applied" && answer.git?.committed, `apply answered ${answer.status}`);
    const file = answer.changes[0].path;
    const [author, authorEmail, committer, committerEmail] = git(state.bundle, "log", "-1", "--format=%an%n%ae%n%cn%n%ce").split("\n");
    assert(author === USERS.editor.name && authorEmail === USERS.editor.email, `author ${author} <${authorEmail}>`);
    assert(committer === SERVER_IDENTITY.name && committerEmail === SERVER_IDENTITY.email, `committer ${committer} <${committerEmail}>`);
    const content = fs.readFileSync(path.join(state.bundle, file), "utf8");
    const by = content.match(/^\s+by:\s*"?([^"\n]+)"?$/m)?.[1];
    assert(by === `human:${USERS.editor.login}`, `generated.by ${by}`);
    const found = toolJson(await result.client.callTool({ name: "search_concepts", arguments: { query: "returns desk" } }));
    assert(found.results?.some((entry) => entry.uri.endsWith("/team/returns-desk")), "new concept not searchable");
    await result.client.close();
    return `commit ${answer.git.commitSha.slice(0, 8)} author "${author} <${authorEmail}>", committer "${committer} <${committerEmail}>", ${file} generated.by ${by}`;
  });
}

// ---------------------------------------------------------------------------
// 3. Users without MCP access never get a token or a redirect
// ---------------------------------------------------------------------------

async function scenario3() {
  const admin = await adminSession();
  const cases = [
    ["3a", "internal", "Internal user without the MCP group"],
    ["3b", "portal", `Portal user (MCP User group held: ${USERS.portal.mcp_group})`],
    ["3c", "admin", "Administrator without the MCP group"],
  ];
  for (const [id, key, label] of cases) {
    await check(id, `${label}: no token, no automatic redirect to the client`, async () => {
      const result = await signInAs(key);
      assert(result.outcome === "page", `sign-in ended with ${result.outcome}`);
      const page = result.authorization.page;
      assert(page.status === 403, `error page status ${page.status}`);
      assert(!page.location, `answer redirects to ${page.location}`);
      assert(!hasOutsideRedirect(result.browser), "a response redirected outside Odoo");
      const text = visibleText(page.html);
      assert(/not allowed to use the MCP server/.test(text), `page says: ${text.slice(0, 120)}`);
      // A forged approval with a valid CSRF token of the same session.
      const csrf = await result.browser.csrfToken();
      assert(csrf, "no CSRF token found for the session");
      const fields = [...result.provider.authorizationUrl.searchParams, ["csrf_token", csrf], ["decision", "approve"]];
      const forged = await result.browser.postForm("/o3p_mcp/oauth/authorize", fields);
      assert(forged.status === 403 && !forged.location, `forged approval answered ${forged.status} ${forged.location || ""}`);
      const codes = await countFor(admin, "o3p.mcp.code", USERS[key].id);
      const tokens = await countFor(admin, "o3p.mcp.token", USERS[key].id);
      assert(codes === 0 && tokens === 0, `${codes} codes and ${tokens} tokens exist for the user`);
      const link = page.html.match(/href="([^"]*error=access_denied[^"]*)"/)?.[1];
      return `403 error page${link ? `, manual "Return to" link only` : ""}; forged POST approve → 403; 0 codes, 0 tokens`;
    });
  }
}

// ---------------------------------------------------------------------------
// 4. Refresh rotation and replay
// ---------------------------------------------------------------------------

async function scenario4() {
  const result = await signInAs("user");
  const client = result.provider.clientInformation();
  const { access_token: a0, refresh_token: r0 } = result.tokens;
  await result.client.close();
  let a1;
  let r1;
  let rotatedAt;
  await check("4a", "Refresh rotates: new pair, old access token still valid, quick retry refused without revoking", async () => {
    assert((await mcpStatus(a0)) === 200, "a0 refused before rotation");
    const refreshed = await oauth.refresh(r0, client);
    rotatedAt = performance.now();
    assert(refreshed.status === 200, `refresh answered ${refreshed.status} ${JSON.stringify(refreshed.body)}`);
    a1 = refreshed.body.access_token;
    r1 = refreshed.body.refresh_token;
    assert(a1 && r1 && a1 !== a0 && r1 !== r0, "the pair did not rotate");
    assert(refreshed.headers.get("cache-control") === "no-store", "token response is cacheable");
    assert((await oauth.introspect(a0)).active === true, "a0 inactive after rotation");
    const retry = await oauth.refresh(r0, client);
    assert(retry.status === 400 && retry.body.error === "invalid_grant", `quick retry answered ${retry.status}`);
    assert((await oauth.introspect(a1)).active === true, "the quick retry revoked the family");
    return `rotation ok; retry within the grace period → invalid_grant, family intact`;
  });
  await check("4b", "Replay of the old refresh token after the grace window revokes the family; okf-mcp refuses within 60 s", async () => {
    assert(a1, "no rotated token");
    assert((await mcpStatus(a1)) === 200, "a1 refused before replay");
    const cachedAt = performance.now();
    const wait = 11_000 - (performance.now() - rotatedAt);
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    const replay = await oauth.refresh(r0, client);
    const revokedAt = performance.now();
    assert(replay.status === 400 && replay.body.error === "invalid_grant", `replay answered ${replay.status}`);
    assert((await oauth.introspect(a1)).active === false, "Odoo still reports a1 active");
    assert((await oauth.introspect(a0)).active === false, "Odoo still reports a0 active");
    const next = await oauth.refresh(r1, client);
    assert(next.status === 400, `r1 still refreshes (${next.status})`);
    const stillCached = (await mcpStatus(a1)) === 200;
    const refusedAt = await waitRefused(a1);
    const sinceRevocation = refusedAt - revokedAt;
    const sinceCache = refusedAt - cachedAt;
    assert(sinceCache <= CACHE_LIMIT_MS + 2_000, `refused ${seconds(sinceCache)} after first introspection`);
    return `Odoo inactive at once; okf-mcp ${stillCached ? "kept accepting (cache) and " : ""}refused ${seconds(sinceRevocation)} after the replay, ${seconds(sinceCache)} after its first introspection of the token`;
  });
}

// ---------------------------------------------------------------------------
// 5. Revocation endpoint
// ---------------------------------------------------------------------------

async function scenario5() {
  // A client of its own, so that the shared client is "another client".
  const result = await signInAs("user", { register: true });
  const client = result.provider.clientInformation();
  const { access_token: access, refresh_token: refresh } = result.tokens;
  await result.client.close();
  await check("5a", "Revoked access token refused by okf-mcp after the cache window; refresh token revoked with it", async () => {
    assert((await mcpStatus(access)) === 200, "token refused before revocation");
    const cachedAt = performance.now();
    const foreign = await oauth.revoke(access, sharedClient);
    assert((await oauth.introspect(access)).active === true, `another client's revocation (${foreign.status}) revoked the token`);
    const revoked = await oauth.revoke(access, client, "access_token");
    const revokedAt = performance.now();
    assert(revoked.status === 200, `revoke answered ${revoked.status}`);
    assert((await oauth.introspect(access)).active === false, "Odoo still reports the token active");
    const refreshed = await oauth.refresh(refresh, client);
    assert(refreshed.status === 400, `refresh after revocation answered ${refreshed.status}`);
    const refusedAt = await waitRefused(access);
    const sinceCache = refusedAt - cachedAt;
    assert(sinceCache <= CACHE_LIMIT_MS + 2_000, `refused ${seconds(sinceCache)} after first introspection`);
    return `foreign client's revoke ignored (HTTP ${foreign.status}); okf-mcp refused ${seconds(refusedAt - revokedAt)} after revocation, ${seconds(sinceCache)} after first introspection`;
  });
  await check("5b", "A never-seen token is refused immediately with a Bearer challenge", async () => {
    const started = performance.now();
    const answer = await mcpPost(MCP_URL, { token: randomToken() });
    const elapsed = performance.now() - started;
    assert(answer.status === 401, `answered ${answer.status}`);
    const challenge = answer.headers.get("www-authenticate") || "";
    assert(/error="invalid_token"/.test(challenge) && /resource_metadata=/.test(challenge), `challenge ${challenge}`);
    return `401 in ${Math.round(elapsed)} ms`;
  });
}

// ---------------------------------------------------------------------------
// 6. Group removal
// ---------------------------------------------------------------------------

async function scenario6() {
  const admin = await adminSession();
  const result = await signInAs("editor2", { scope: "okf.read okf.write" });
  const client = result.provider.clientInformation();
  const { access_token: access, refresh_token: refresh } = result.tokens;
  const userId = USERS.editor2.id;
  let narrowed;
  await check("6a", "Editor group removed: introspection and the next refresh carry okf.read only; write tools disappear for the new token", async () => {
    assert(result.tokens.scope === "okf.read okf.write", `granted ${result.tokens.scope}`);
    assert(toolNames(await result.client.listTools()).includes("okf_apply_changes"), "write tools missing before removal");
    await admin.callKw("res.users", "write", [[userId], { group_ids: [[3, setup.groups.mcp_editor], [4, setup.groups.mcp_user]] }]);
    state.editorRemovedAt = performance.now();
    const answer = await oauth.introspect(access);
    assert(answer.active === true && answer.scope === "okf.read", `introspection: ${JSON.stringify(answer)}`);
    const refreshed = await oauth.refresh(refresh, client);
    assert(refreshed.status === 200 && refreshed.body.scope === "okf.read", `refresh: ${refreshed.status} ${JSON.stringify(refreshed.body)}`);
    narrowed = refreshed.body;
    const fresh = await connectWithToken(MCP_URL, narrowed.access_token);
    const tools = toolNames(await fresh.client.listTools());
    await fresh.client.close();
    assert(!tools.some((name) => WRITE_TOOLS.includes(name)), "write tools still listed for the new token");
    return `introspection scope "okf.read", refresh scope "okf.read", new token sees no write tools`;
  });
  await check("6b", "Editor group removed: the old access token loses write tools once okf-mcp's cache expires", async () => {
    const head = git(state.bundle, "rev-parse", "HEAD");
    let late;
    try {
      late = toolJson(
        await result.client.callTool({
          name: "okf_apply_changes",
          arguments: {
            bundle: "shop",
            message: "docs: written after losing the editor group",
            changes: [{ op: "create", type: "Note", title: "Late write", body: "# Late write\n\nWritten from a cached token.\n" }],
          },
        }),
      );
    } catch (error) {
      late = { error: error.message };
    }
    const lateWrite = late?.status === "applied";
    info("6b", `okf_apply_changes with the cached old token right after the group removal: ${lateWrite ? `APPLIED (commit ${late.git?.commitSha?.slice(0, 8)}, author ${git(state.bundle, "log", "-1", "--format=%an")})` : `refused (${JSON.stringify(late).slice(0, 80)})`} — documented: answers are cached up to 60 s`);
    if (!lateWrite) assert(git(state.bundle, "rev-parse", "HEAD") === head, "HEAD moved");
    const goneAt = await waitFor(
      async () => {
        const session = await connectWithToken(MCP_URL, access).catch(() => null);
        if (!session) return false;
        const tools = toolNames(await session.client.listTools());
        await session.client.close();
        return !tools.includes("okf_apply_changes");
      },
      { timeoutMs: 90_000 },
    );
    const elapsed = goneAt - state.editorRemovedAt;
    assert(elapsed <= CACHE_LIMIT_MS + 5_000, `write tools still visible ${seconds(elapsed)} after the removal`);
    return `write tools gone ${seconds(elapsed)} after the group removal`;
  });
  await check("6c", "MCP User group removed too: tokens inactive, refresh refused", async () => {
    await admin.callKw("res.users", "write", [[userId], { group_ids: [[3, setup.groups.mcp_user]] }]);
    assert((await oauth.introspect(narrowed.access_token)).active === false, "token still active");
    const refreshed = await oauth.refresh(narrowed.refresh_token, client);
    assert(refreshed.status === 400 && refreshed.body.error === "invalid_grant", `refresh answered ${refreshed.status}`);
    return "introspection inactive, refresh invalid_grant";
  });
  await result.client.close();
}

// ---------------------------------------------------------------------------
// 7. Audience
// ---------------------------------------------------------------------------

async function scenario7() {
  await check("7a", "A token issued for this MCP server is refused by another MCP server trusting the same Odoo", async () => {
    const token = state.user.tokens.access_token;
    assert((await mcpStatus(token, MCP_URL)) === 200, "token refused by its own server");
    const answer = await mcpPost(MCP2_URL, { token });
    assert(answer.status === 401, `second server answered ${answer.status}`);
    return `aud ${MCP_URL}: accepted there, 401 at ${MCP2_URL}`;
  });
  await check("7b", "A client asking Odoo for a token for another resource gets an error page, no code, no redirect", async () => {
    const result = await timedSignIn({ mcpUrl: MCP2_URL, issuer: ISSUER, login: USERS.user.login, password: PASSWORD, clientInformation: sharedClient });
    assert(result.outcome === "page", `outcome ${result.outcome}`);
    assert(result.provider.authorizationUrl.searchParams.get("resource") === MCP2_URL, "the SDK did not send resource");
    const text = visibleText(result.authorization.page.html);
    assert(result.authorization.page.status === 400 && /only issued for/.test(text), `page ${result.authorization.page.status}: ${text.slice(0, 100)}`);
    assert(!hasOutsideRedirect(result.browser), "redirected to the client");
    return "invalid_target shown on Odoo's error page";
  });
  await check("7c", "Code exchange naming another resource is refused (invalid_target)", async () => {
    const flow = await oauth.codeFlow({
      browser: new Browser(ISSUER),
      login: USERS.user.login,
      password: PASSWORD,
      client: sharedClient,
      redirectUri: sharedClient.redirect_uris[0],
      resource: MCP_URL,
      exchange: { resource: MCP2_URL },
    });
    assert(flow.exchange?.status === 400 && flow.exchange.body.error === "invalid_target", `exchange answered ${flow.exchange?.status} ${JSON.stringify(flow.exchange?.body)}`);
    return "400 invalid_target";
  });
  await check("7d", "A token Odoo issued for a different audience is refused by okf-mcp (and accepted by its own server)", async () => {
    const admin = await adminSession();
    let result;
    try {
      await admin.saveSettings({ o3p_mcp_resource_url: MCP2_URL });
      result = await timedSignIn({ mcpUrl: MCP2_URL, issuer: ISSUER, login: USERS.user.login, password: PASSWORD, clientInformation: sharedClient });
    } finally {
      await admin.saveSettings({ o3p_mcp_resource_url: MCP_URL });
    }
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    const token = result.tokens.access_token;
    const answer = await oauth.introspect(token);
    assert(answer.active && answer.aud === MCP2_URL, `introspection ${JSON.stringify(answer)}`);
    assert(toolNames(await result.client.listTools()).includes("search_concepts"), "own server refused it");
    await result.client.close();
    const status = await mcpStatus(token, MCP_URL);
    assert(status === 401, `okf-mcp answered ${status}`);
    return `aud ${MCP2_URL}: 401 at ${MCP_URL}`;
  });
}

// ---------------------------------------------------------------------------
// 8. Health button
// ---------------------------------------------------------------------------

// A loopback port nothing listens on (bound once and released).
function unusedPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function pressHealth(session, values = {}) {
  const id = await session.callKw("res.config.settings", "create", [values]);
  return session.callButton("res.config.settings", "action_o3p_mcp_check_health", [id]);
}

async function scenario8() {
  const admin = await adminSession();
  await check("8a", "Settings › MCP › Check MCP server reaches okf-mcp's /health", async () => {
    const action = await pressHealth(admin);
    const { message, type } = action.params;
    assert(type === "success" && message === `${MCP_ORIGIN}/health answered HTTP 200.`, `${type}: ${message}`);
    return `"${message}"`;
  });
  await check("8b", "Check MCP server reports an unreachable URL (negative control, settings unchanged)", async () => {
    const action = await pressHealth(admin, { o3p_mcp_resource_url: `http://localhost:${await unusedPort()}/mcp` });
    assert(action.params.type === "danger" && /could not be reached/.test(action.params.message), `${action.params.type}: ${action.params.message}`);
    const again = await pressHealth(admin);
    assert(again.params.type === "success", "the configured URL changed");
    return `"${action.params.message}"`;
  });
  await check("8c", "A non-administrator cannot press Check MCP server", async () => {
    const editor = await new OdooSession(ISSUER).login(USERS.editor.login, PASSWORD);
    try {
      await pressHealth(editor);
    } catch (error) {
      return `${error.name}`;
    }
    throw new Error("the editor pressed the button");
  });
}

// ---------------------------------------------------------------------------
// 9. What real clients do differently
// ---------------------------------------------------------------------------

async function scenario9() {
  await check("9a", "Spec-following client (scope from the 401 challenge) signs an editor in with okf.write", async () => {
    const result = await signInAs("editor");
    const requested = result.provider.authorizationUrl.searchParams.get("scope");
    const challenge = result.trace[0].wwwAuthenticate?.match(/scope="([^"]*)"/)?.[1];
    const tools = result.client ? toolNames(await result.client.listTools()) : [];
    await result.client?.close();
    assert(
      result.tokens?.scope?.includes("okf.write") && tools.includes("okf_apply_changes"),
      `401 challenge scope="${challenge}" → SDK requested "${requested}" → granted "${result.tokens?.scope}"; okf_apply_changes ${tools.includes("okf_apply_changes") ? "listed" : "absent"} — editors cannot write through MCP SDK clients`,
    );
    return `requested "${requested}"`;
  });

  await check("9a2", "A request without credentials gets a Bearer challenge without an error code (RFC 6750 §3.1)", async () => {
    const answer = await mcpPost(MCP_URL, {});
    const challenge = answer.headers.get("www-authenticate") || "";
    assert(answer.status === 401 && /^Bearer /.test(challenge) && /resource_metadata="/.test(challenge), `answered ${answer.status} ${challenge}`);
    assert(!/error=/.test(challenge), `challenge carries an error: ${challenge}`);
    return `WWW-Authenticate: ${challenge.replace(/resource_metadata="[^"]*"/, 'resource_metadata="…"')}`;
  });

  // Registrations the way hosted connectors do them.
  const hosted = [
    ["9b", "Claude-like connector (https callback, public client, resource sent)", MCP_URL, { client_name: "Claude", redirect_uris: ["https://claude.ai/api/mcp/auth_callback"], grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none", scope: "okf.read okf.write" }],
    ["9c", "ChatGPT-like connector (https callback, client_secret_post, extra metadata, no resource parameter)", undefined, { client_name: "ChatGPT", redirect_uris: ["https://chatgpt.com/connector_platform_oauth_redirect"], grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "client_secret_post", application_type: "web", client_uri: "https://chatgpt.com", logo_uri: "https://chatgpt.com/logo.png", software_id: "e2e-test" }],
  ];
  for (const [id, label, resource, body] of hosted) {
    await check(id, `${label}: registers, consents, exchanges server-side, token works at okf-mcp`, async () => {
      const registered = await oauth.register(body);
      assert(registered.status === 201, `registration ${registered.status} ${JSON.stringify(registered.body)}`);
      const flow = await oauth.codeFlow({
        browser: new Browser(ISSUER),
        login: USERS.editor.login,
        password: PASSWORD,
        client: registered.body,
        redirectUri: body.redirect_uris[0],
        resource,
        scope: "okf.read okf.write",
      });
      state.hosted = { client: registered.body, tokens: flow.exchange?.body };
      assert(flow.callback?.href.startsWith(`${body.redirect_uris[0]}?`), `redirect ${flow.callback?.href}`);
      assert(flow.exchange?.status === 200, `exchange ${flow.exchange?.status} ${JSON.stringify(flow.exchange?.body)}`);
      const session = await connectWithToken(MCP_URL, flow.exchange.body.access_token);
      const tools = toolNames(await session.client.listTools());
      await session.client.close();
      assert(tools.includes("okf_apply_changes"), "write tools missing");
      return `auth method ${registered.body.token_endpoint_auth_method}, scope "${flow.exchange.body.scope}"`;
    });
  }

  await check("9d", "SDK client registering without token_endpoint_auth_method (RFC 7591 default client_secret_basic)", async () => {
    const result = await signInAs("user", { register: true, clientMetadata: { token_endpoint_auth_method: undefined } });
    const info = result.provider.clientInformation();
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    assert(info.token_endpoint_auth_method === "client_secret_basic" && info.client_secret, `registered ${info.token_endpoint_auth_method}`);
    await result.client.close();
    return "registered client_secret_basic, SDK authenticated with it";
  });

  await check("9e", "MCP URL configured with a trailing slash (/mcp/)", async () => {
    const result = await signInAs("user", { mcpUrl: `${MCP_URL}/` });
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    const tools = toolNames(await result.client.listTools());
    await result.client.close();
    assert(tools.includes("search_concepts"), "tools missing");
    return `resource sent: ${result.provider.authorizationUrl.searchParams.get("resource")}`;
  });

  await check("9f", "Second sign-in in the same browser reuses the Odoo session (no login page)", async () => {
    const browser = new Browser(ISSUER);
    const first = await signInAs("user", { browser });
    await first.client.close();
    browser.trace.length = 0;
    const second = await signInAs("user", { browser });
    assert(second.outcome === "signed-in", `sign-in ended with ${second.outcome}`);
    await second.client.close();
    assert(!browser.trace.some((entry) => new URL(entry.url).pathname === "/web/login"), "login page shown again");
    return `straight to consent in ${Math.round(second.timings.browser.toConsentMs)} ms`;
  });

  await check("9g", "User denies: redirect with error=access_denied, state and iss, no code", async () => {
    const result = await signInAs("user", { decision: "deny" });
    const params = result.callback?.searchParams;
    assert(params, `outcome ${result.outcome}`);
    assert(params.get("error") === "access_denied" && !params.get("code"), `callback ${result.callback.search}`);
    assert(params.get("iss") === ISSUER && params.get("state") === result.provider.state(), "state or iss missing");
    return result.callback.search.replace(/state=[^&]+/, "state=…");
  });

  await check("9h", "Missing or plain PKCE from a self-registered client: error page, no redirect", async () => {
    const outcomes = [];
    for (const variant of [{}, { code_challenge: "x".repeat(43), code_challenge_method: "plain" }]) {
      const browser = new Browser(ISSUER);
      const url = oauth.authorizeUrl({ response_type: "code", client_id: sharedClient.client_id, redirect_uri: sharedClient.redirect_uris[0], state: "s", resource: MCP_URL, ...variant });
      const answer = await browser.authorize(url, { login: USERS.user.login, password: PASSWORD });
      assert(answer.outcome === "page" && answer.page.status === 400, `outcome ${answer.outcome} ${answer.page?.status}`);
      assert(!hasOutsideRedirect(browser), "redirected to the client");
      outcomes.push(answer.page.status);
    }
    return `HTTP ${outcomes.join(", ")}`;
  });

  await check("9i", "Native client on another loopback port (RFC 8252 §7.3) completes the flow", async () => {
    const flow = await oauth.codeFlow({
      browser: new Browser(ISSUER),
      login: USERS.user.login,
      password: PASSWORD,
      client: sharedClient,
      redirectUri: "http://127.0.0.1:43211/callback",
      resource: MCP_URL,
    });
    assert(flow.callback?.href.startsWith("http://127.0.0.1:43211/callback?"), `redirect ${flow.callback?.href}`);
    assert(flow.exchange?.status === 200, `exchange ${flow.exchange?.status}`);
    return "redirected to port 43211, exchange 200";
  });

  await check("9j", "SDK refreshes by itself when okf-mcp answers 401 (lost or stale access token)", async () => {
    const result = await signInAs("user");
    const before = result.provider.savedTokens.length;
    result.provider.saveTokens({ ...result.provider.tokens(), access_token: randomToken() });
    const found = toolJson(await result.client.callTool({ name: "search_concepts", arguments: { query: "sla" } }));
    await result.client.close();
    assert(found.results?.length, "search failed after refresh");
    const refreshes = result.trace.filter((entry) => entry.url.endsWith("/oauth/token")).length;
    assert(refreshes === 2 && result.provider.savedTokens.length === before + 2, `token requests ${refreshes}`);
    return "401 → refresh_token grant → retried call succeeded";
  });

  await check("9k", "Five concurrent refreshes with one refresh token: one wins, the family survives", async () => {
    const result = await signInAs("user");
    await result.client.close();
    const client = result.provider.clientInformation();
    const answers = await Promise.all(Array.from({ length: 5 }, () => oauth.refresh(result.tokens.refresh_token, client)));
    const statuses = answers.map((answer) => answer.status);
    const winners = answers.filter((answer) => answer.status === 200);
    assert(winners.length === 1, `statuses ${statuses.join(",")}`);
    assert(answers.every((answer) => answer.status === 200 || answer.body.error === "invalid_grant"), `statuses ${statuses.join(",")}`);
    const alive = (await oauth.introspect(winners[0].body.access_token)).active;
    assert(alive, "the winner's token was revoked by a concurrent loser");
    return `statuses ${statuses.join(",")}, winner active`;
  });

  await check("9l", "Three concurrent exchanges of one code: exactly one succeeds", async () => {
    const browser = new Browser(ISSUER);
    const { verifier, challenge } = pkce();
    const redirectUri = sharedClient.redirect_uris[0];
    const answer = await browser.authorize(
      oauth.authorizeUrl({ response_type: "code", client_id: sharedClient.client_id, redirect_uri: redirectUri, state: "s", code_challenge: challenge, code_challenge_method: "S256", resource: MCP_URL }),
      { login: USERS.user.login, password: PASSWORD },
    );
    const code = new URL(answer.callback).searchParams.get("code");
    const params = { grant_type: "authorization_code", code, code_verifier: verifier, redirect_uri: redirectUri };
    const answers = await Promise.all(Array.from({ length: 3 }, () => oauth.token(params, sharedClient)));
    const statuses = answers.map((entry) => entry.status);
    const winners = answers.filter((entry) => entry.status === 200);
    assert(winners.length === 1, `statuses ${statuses.join(",")}`);
    const alive = (await oauth.introspect(winners[0].body.access_token)).active;
    info("9l", `winner's token after the concurrent duplicates: ${alive ? "active" : "revoked (a duplicate counts as code reuse)"}`);
    return `statuses ${statuses.join(",")}`;
  });

  await check("9m", "Burst of 12 unknown tokens: all refused (401, or 503 with Retry-After beyond 8 in flight)", async () => {
    const answers = await Promise.all(Array.from({ length: 12 }, () => mcpPost(MCP_URL, { token: randomToken() })));
    const counts = {};
    for (const answer of answers) counts[answer.status] = (counts[answer.status] || 0) + 1;
    assert(answers.every((answer) => answer.status === 401 || (answer.status === 503 && answer.headers.get("retry-after"))), JSON.stringify(counts));
    return Object.entries(counts).map(([status, count]) => `${count}×${status}`).join(", ");
  });

  await check("9n", "Token in the query string is ignored; scheme name is case-insensitive", async () => {
    const token = state.user.tokens.access_token;
    const query = await fetch(`${MCP_URL}?access_token=${encodeURIComponent(token)}`, { method: "POST", headers: { "content-type": "application/json", accept: "application/json, text/event-stream" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }) });
    assert(query.status === 401, `query token answered ${query.status}`);
    const lower = await mcpPost(MCP_URL, { token, scheme: "bearer" });
    info("9n", `"Authorization: bearer <token>" answered ${lower.status}`);
    return `query → 401, lowercase scheme → ${lower.status}`;
  });

  await check("9o", "Odoo's OAuth endpoints never set a session cookie", async () => {
    const urls = [];
    const probe = async (label, response) => {
      if (response.headers.get("set-cookie")) urls.push(label);
    };
    await probe("metadata", await fetch(`${ISSUER}/.well-known/oauth-authorization-server`));
    await probe("token", await fetch(`${ISSUER}/o3p_mcp/oauth/token`, { method: "POST", body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: "x", client_id: sharedClient.client_id }) }));
    await probe("revoke", await fetch(`${ISSUER}/o3p_mcp/oauth/revoke`, { method: "POST", body: new URLSearchParams({ token: "x", client_id: sharedClient.client_id }) }));
    await probe("introspect", await fetch(`${ISSUER}/o3p_mcp/oauth/introspect`, { method: "POST", body: new URLSearchParams({ token: "x" }) }));
    assert(urls.length === 0, `Set-Cookie on ${urls.join(", ")}`);
    return "metadata, token, revoke, introspect: no Set-Cookie";
  });

  await check("9u", "Consent page escapes a hostile client name and state, and shows a look-alike host as xn--", async () => {
    const registered = await oauth.register({
      client_name: '<img src=x onerror=alert(1)>Evil "app"',
      redirect_uris: ["https://\u0430pple.com/cb"],
      token_endpoint_auth_method: "none",
    });
    assert(registered.status === 201, `registration ${registered.status} ${JSON.stringify(registered.body)}`);
    const { challenge } = pkce();
    const hostileState = '"><script>alert(1)</script>';
    const answer = await new Browser(ISSUER).authorize(
      oauth.authorizeUrl({ response_type: "code", client_id: registered.body.client_id, redirect_uri: "https://\u0430pple.com/cb", state: hostileState, code_challenge: challenge, code_challenge_method: "S256", resource: MCP_URL }),
      { login: USERS.user.login, password: PASSWORD, decision: "none" },
    );
    assert(answer.outcome === "consent", `outcome ${answer.outcome}`);
    const html = answer.consent.html;
    assert(!html.includes("<img src=x") && !html.includes("<script>alert(1)"), "unescaped markup on the consent page");
    assert(visibleText(html).includes("xn--pple-43d.com"), `redirect host not shown as xn--: ${visibleText(html).match(/sent to (\S+)/)?.[1]}`);
    assert(answer.consent.form.inputs.some(([name, value]) => name === "state" && value === hostileState), "state not carried verbatim");
    return "markup escaped, host shown as xn--pple-43d.com, state carried verbatim";
  });

  await check("9v", "Unregistered redirect URI variants show an error page and never redirect", async () => {
    const registeredUri = sharedClient.redirect_uris[0];
    const variants = [
      `${registeredUri}?next=https://evil.example`,
      registeredUri.replace("/callback", "/callback/../evil"),
      registeredUri.replace("http://127.0.0.1", "http://localhost"),
      registeredUri.replace("http://", "https://"),
      "https://evil.example/callback",
    ];
    for (const redirectUri of variants) {
      const browser = new Browser(ISSUER);
      const { challenge } = pkce();
      const answer = await browser.authorize(
        oauth.authorizeUrl({ response_type: "code", client_id: sharedClient.client_id, redirect_uri: redirectUri, state: "s", code_challenge: challenge, code_challenge_method: "S256", resource: MCP_URL }),
        { login: USERS.user.login, password: PASSWORD },
      );
      assert(answer.outcome === "page" && answer.page.status === 400, `${redirectUri}: ${answer.outcome} ${answer.page?.status}`);
      assert(!hasOutsideRedirect(browser), `${redirectUri}: redirected`);
    }
    return `${variants.length} variants refused`;
  });

  await check("9w", "Wrong token types and wrong clients are refused", async () => {
    const tokens = state.user.tokens;
    assert((await mcpStatus(tokens.refresh_token)) === 401, "refresh token accepted as bearer");
    const asRefresh = await oauth.refresh(tokens.access_token, sharedClient);
    assert(asRefresh.status === 400 && asRefresh.body.error === "invalid_grant", `access token as refresh: ${asRefresh.status}`);
    const foreign = await oauth.refresh(tokens.refresh_token, state.hosted.client);
    assert(foreign.status === 400 && foreign.body.error === "invalid_grant", `another client's refresh: ${foreign.status}`);
    assert((await oauth.introspect(tokens.access_token)).active, "a foreign refresh attempt revoked the family");
    const { client_id: id, client_secret: secret } = state.hosted.client;
    const asClient = await fetch(`${ISSUER}/o3p_mcp/oauth/introspect`, {
      method: "POST",
      headers: { authorization: `Basic ${Buffer.from(`${id}:${secret}`).toString("base64")}`, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: tokens.access_token }),
    });
    assert(asClient.status === 401, `MCP client credentials at introspection: ${asClient.status}`);
    return "refresh token as bearer → 401; access token as refresh → invalid_grant; other client's refresh → invalid_grant (family kept); MCP client at introspection → 401";
  });

  await check("9x", "A mistyped password keeps the authorization request: the second attempt lands on the consent page", async () => {
    const browser = new Browser(ISSUER);
    const { challenge } = pkce();
    const url = oauth.authorizeUrl({ response_type: "code", client_id: sharedClient.client_id, redirect_uri: sharedClient.redirect_uris[0], state: "s", code_challenge: challenge, code_challenge_method: "S256", resource: MCP_URL });
    const loginPage = await browser.navigate(url);
    const form = parseForms(loginPage.html).find((candidate) => candidate.inputs.some(([name]) => name === "password"));
    const redirect = new URL(browser.trace.at(-1).url).searchParams.get("redirect");
    const fields = { ...Object.fromEntries(form.inputs), login: USERS.user.login, password: "wrong", redirect };
    const failed = await browser.postForm("/web/login", fields);
    assert(failed.status === 200 && !failed.location, `wrong password answered ${failed.status}`);
    const retryForm = parseForms(failed.html).find((candidate) => candidate.inputs.some(([name]) => name === "password"));
    const retryFields = Object.fromEntries(retryForm.inputs);
    assert(retryFields.redirect === redirect, `the retry form lost the redirect (${retryFields.redirect})`);
    const landed = await browser.navigate((await browser.postForm("/web/login", { ...retryFields, login: USERS.user.login, password: PASSWORD })).location);
    assert(parseForms(landed.html).some((candidate) => candidate.inputs.some(([name, value]) => name === "decision" && value === "approve")), "not on the consent page");
    return "redirect kept in the login form after the failed attempt";
  });

  await check("9y", "okf-mcp refuses a foreign Host header (DNS rebinding)", async () => {
    const http = await import("node:http");
    const status = await new Promise((resolve, reject) => {
      const request = http.request(MCP_URL, { method: "POST", headers: { host: "evil.example:18766", "content-type": "application/json", authorization: `Bearer ${state.user.tokens.access_token}` } }, (response) => {
        response.resume();
        resolve(response.statusCode);
      });
      request.on("error", reject);
      request.end(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }));
    });
    assert(status === 403, `answered ${status}`);
    return "403";
  });

  await check("9z", "Archiving a client in Odoo deactivates its tokens at once (Odoo side)", async () => {
    const admin = await adminSession();
    const ids = await admin.callKw("o3p.mcp.client", "search", [[["client_id", "=", state.hosted.client.client_id]]]);
    assert(ids.length === 1, "client not found");
    assert((await oauth.introspect(state.hosted.tokens.access_token)).active, "token inactive before archival");
    await admin.callKw("o3p.mcp.client", "write", [ids, { active: false }]);
    assert((await oauth.introspect(state.hosted.tokens.access_token)).active === false, "token still active");
    const refreshed = await oauth.refresh(state.hosted.tokens.refresh_token, state.hosted.client);
    assert(refreshed.status === 401 && refreshed.body.error === "invalid_client", `refresh answered ${refreshed.status}`);
    return "introspection inactive, refresh invalid_client";
  });

  await check("9z2", "A request adding scopes Odoo does not know (offline_access, profile) is served with the known ones", async () => {
    const flow = await oauth.codeFlow({
      browser: new Browser(ISSUER),
      login: USERS.editor.login,
      password: PASSWORD,
      client: sharedClient,
      redirectUri: sharedClient.redirect_uris[0],
      resource: MCP_URL,
      scope: "okf.read okf.write offline_access profile",
    });
    assert(flow.exchange?.status === 200, `outcome ${flow.authorization.outcome} ${flow.authorization.page?.status ?? ""} ${flow.exchange?.status ?? ""}`);
    assert(flow.exchange.body.scope === "okf.read okf.write", `granted ${flow.exchange.body.scope}`);
    return `granted "${flow.exchange.body.scope}"`;
  });

  await checkCors();
}

// MCP Inspector runs in a browser at http://localhost:6274: every request it
// makes is subject to CORS.
async function checkCors() {
  const origin = "http://localhost:6274";
  const prmUrl = `${MCP_ORIGIN}/.well-known/oauth-protected-resource/mcp`;
  const steps = [
    ["POST /mcp (content-type, protocol version)", MCP_URL, "POST", ["content-type", "mcp-protocol-version"]],
    ["POST /mcp with Authorization", MCP_URL, "POST", ["authorization", "content-type", "mcp-protocol-version"]],
    ["GET protected-resource metadata with MCP-Protocol-Version", prmUrl, "GET", ["mcp-protocol-version"]],
    ["GET AS metadata with MCP-Protocol-Version", `${ISSUER}/.well-known/oauth-authorization-server`, "GET", ["mcp-protocol-version"]],
    ["POST register (JSON)", `${ISSUER}/o3p_mcp/oauth/register`, "POST", ["content-type"]],
    ["POST token with Basic auth", `${ISSUER}/o3p_mcp/oauth/token`, "POST", ["authorization"]],
    ["POST revoke", `${ISSUER}/o3p_mcp/oauth/revoke`, "POST", ["content-type"]],
  ];
  const report = [];
  for (const [label, url, method, headers] of steps) {
    const answer = await preflight(url, origin, method, headers);
    report.push({ label, ...answer });
    info("9p", `preflight ${label}: ${answer.ok ? "allowed" : "BLOCKED"} (HTTP ${answer.status}, allow-origin ${answer.allowOrigin}, allow-headers "${answer.allowHeaders}")`);
  }
  await check("9p", "Browser client (MCP Inspector origin): CORS preflights for the MCP endpoint, registration and token pass", async () => {
    const blocked = report.filter((entry) => !entry.ok && !entry.label.includes("metadata"));
    assert(blocked.length === 0, `blocked: ${blocked.map((entry) => entry.label).join("; ")}`);
    const challenge = await fetch(MCP_URL, { method: "POST", headers: { origin, "content-type": "application/json" }, body: "{}" });
    const expose = challenge.headers.get("access-control-expose-headers") || "";
    assert(challenge.status === 401 && challenge.headers.get("access-control-allow-origin") === origin && /www-authenticate/i.test(expose), "the 401 challenge is not readable cross-origin");
    return "401 challenge readable (WWW-Authenticate exposed)";
  });
  await check("9q", "Browser client: okf-mcp answers the protected-resource metadata preflight (MCP-Protocol-Version allowed)", async () => {
    const entry = report.find((item) => item.label.includes("protected"));
    assert(entry.ok, `preflight HTTP ${entry.status}, allow-origin ${entry.allowOrigin}, allow-headers "${entry.allowHeaders}"`);
    return `HTTP ${entry.status}, allow-headers "${entry.allowHeaders}"`;
  });
  await check("9q2", "Browser client: Odoo answers the authorization-server metadata preflight (MCP-Protocol-Version allowed)", async () => {
    const entry = report.find((item) => item.label.startsWith("GET AS metadata"));
    assert(entry.ok, `preflight HTTP ${entry.status}, allow-origin ${entry.allowOrigin}, allow-headers "${entry.allowHeaders}"`);
    return `HTTP ${entry.status}, allow-headers "${entry.allowHeaders}"`;
  });
  await check("9r", "Foreign browser origin is refused by okf-mcp (403, no CORS headers)", async () => {
    const answer = await fetch(MCP_URL, { method: "POST", headers: { origin: "https://evil.example", "content-type": "application/json", authorization: `Bearer ${state.user.tokens.access_token}` }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }) });
    assert(answer.status === 403 && !answer.headers.get("access-control-allow-origin"), `answered ${answer.status}`);
    return "403";
  });
}

// Access token expiry with a 1-minute lifetime; the SDK refreshes.
async function expiryPrepare() {
  const admin = await adminSession();
  let result;
  try {
    await admin.saveSettings({ o3p_mcp_access_token_minutes: 1 });
    result = await signInAs("user");
  } finally {
    await admin.saveSettings({ o3p_mcp_access_token_minutes: 60 });
  }
  return result;
}

async function expiryCheck(result) {
  await check("9s", "Expired access token (1-minute lifetime) is refused at exp; the SDK client refreshes and continues", async () => {
    assert(result.outcome === "signed-in", `sign-in ended with ${result.outcome}`);
    assert(result.tokens.expires_in === 60, `expires_in ${result.tokens.expires_in}`);
    const { exp } = await oauth.introspect(result.tokens.access_token);
    assert(toolNames(await result.client.listTools()).length > 0, "listing failed");
    const refusedAt = await waitFor(async () => (await mcpStatus(result.tokens.access_token)) === 401, { timeoutMs: 90_000, intervalMs: 500 });
    const lateBy = Date.now() - exp * 1000 - (performance.now() - refusedAt);
    assert(lateBy < 2_000, `refused ${seconds(lateBy)} after exp`);
    const found = toolJson(await result.client.callTool({ name: "search_concepts", arguments: { query: "order" } }));
    await result.client.close();
    assert(found.results?.length && result.provider.savedTokens.length === 2, "the SDK did not refresh");
    return `refused ${lateBy >= 0 ? `${Math.round(lateBy)} ms after` : `${Math.round(-lateBy)} ms before`} exp; SDK refreshed on the 401`;
  });
}

// Registration limit (last: it uses up the address's registrations).
async function registrationLimit() {
  await check("9t", "Registration limit (a setting, here lowered to 20/hour): 429 with Retry-After from one address; X-Forwarded-For cannot dodge it", async () => {
    const admin = await adminSession();
    await admin.saveSettings({ o3p_mcp_registration_limit: 20 });
    let created = 0;
    let limited;
    for (let attempt = 0; attempt < 25; attempt += 1) {
      const answer = await oauth.register({ client_name: `burst ${attempt}`, redirect_uris: ["http://127.0.0.1:18999/callback"], token_endpoint_auth_method: "none" });
      if (answer.status === 201) created += 1;
      else {
        limited = answer;
        break;
      }
    }
    assert(limited?.status === 429 && limited.headers.get("retry-after"), `no 429 after ${created} registrations`);
    const spoofed = await fetch(`${ISSUER}/o3p_mcp/oauth/register`, { method: "POST", headers: { "content-type": "application/json", "x-forwarded-for": "203.0.113.7" }, body: JSON.stringify({ redirect_uris: ["http://127.0.0.1:18999/callback"] }) });
    assert(spoofed.status === 429, `spoofed X-Forwarded-For answered ${spoofed.status}`);
    await admin.saveSettings({ o3p_mcp_registration_limit: 200 });
    const again = await oauth.register({ client_name: "after raising the limit", redirect_uris: ["http://127.0.0.1:18999/callback"], token_endpoint_auth_method: "none" });
    assert(again.status === 201, `after raising the limit: ${again.status}`);
    return `429 after ${created} more registrations in this run; spoofed X-Forwarded-For → 429; raised back to 200 → 201`;
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const work = argv.work;
  state.project = createProject(path.join(work, "project"), { issuer: ISSUER, database: "o3p_e2e" });
  state.bundle = state.project.shop;
  const otherBundle = createBundle(path.join(work, "other"));
  if (sales) {
    await check("D-p", "The okf-odoo producer generates the `odoo` bundle into the okf project once", async () => {
      const { receipt, ms } = runProducer(state.project, sales.producer_api_key);
      assert(receipt.applied && receipt.valid, `receipt ${JSON.stringify(receipt).slice(0, 200)}`);
      const s = receipt.summary;
      return `${s.files} files (${s.models} models, ${s.fields} fields, ${s.groups} groups, ${s.accessEntries} access entries) in ${Math.round(ms)} ms`;
    });
  }
  const common = { issuer: ISSUER, introspection: setup.introspection };
  const servers = [];
  try {
    const primary = await startOkf({
      ...common,
      project: state.project.file,
      port: MCP_PORT,
      publicUrl: MCP_URL,
      write: true,
      env: { [ENV.clientId]: setup.introspection.client_id, [ENV.clientSecret]: setup.introspection.client_secret },
    });
    servers.push(primary);
    info("okf", `node ${primary.args.join(" ")}`);
    servers.push(await startOkf({ ...common, root: otherBundle, port: MCP2_PORT, publicUrl: MCP2_URL }));

    await flowCheck();
    if (!sharedClient) throw new Error("the first sign-in failed; nothing else can run");
    if (wanted(1)) await scenario1();
    else state.user = await signInAs("user");
    if (wanted(2)) await scenario2();
    if (wanted(3)) await scenario3();
    if (wanted(7)) await scenario7();
    if (wanted(8)) await scenario8();
    if (wanted(9)) await scenario9();
    const extended = {
      check,
      info,
      assert,
      setup,
      sales,
      oauth,
      state,
      ISSUER,
      MCP_URL,
      toolJson,
      mcpPost,
      adminSession,
      sharedClient: () => sharedClient,
      signInAs: (login, options = {}) =>
        timedSignIn({ mcpUrl: MCP_URL, issuer: ISSUER, login, password: PASSWORD, clientInformation: sharedClient, ...options }),
    };
    if (sales && wanted("e")) await runE4(extended);
    if (sales && wanted("d")) {
      await runD4({
        check,
        info,
        assert,
        setup,
        sales,
        oauth,
        state,
        ISSUER,
        MCP_URL,
        toolJson,
        mcpPost,
        adminSession,
        sharedClient: () => sharedClient,
        signInAs: (login, options = {}) =>
          timedSignIn({ mcpUrl: MCP_URL, issuer: ISSUER, login, password: PASSWORD, clientInformation: sharedClient, ...options }),
      });
    }
    // The checks that wait for okf-mcp's cache run side by side.
    const expiry = wanted(9) ? await expiryPrepare() : null;
    await Promise.all([
      wanted(4) && scenario4(),
      wanted(5) && scenario5(),
      wanted(6) && scenario6(),
      expiry && expiryCheck(expiry),
    ]);
    if (wanted(9)) await registrationLimit();
    await state.user.client?.close();
  } finally {
    for (const server of servers) await server.stop();
  }
  if (wanted(8)) {
    await check("8d", "With okf-mcp stopped, Check MCP server reports it unreachable", async () => {
      const action = await pressHealth(await adminSession());
      assert(action.params.type === "danger", `${action.params.type}: ${action.params.message}`);
      return `"${action.params.message}"`;
    });
  }

  if (signInTimes.length) {
    const totals = signInTimes.map((t) => t.totalMs).sort((a, b) => a - b);
    const logins = signInTimes.map((t) => t.browser?.loginMs).filter(Boolean).sort((a, b) => a - b);
    const median = (list) => Math.round(list[Math.floor(list.length / 2)]);
    info("time", `${totals.length} complete sign-ins (first request → token → MCP connected): median ${median(totals)} ms, max ${Math.round(totals.at(-1))} ms; /web/login step median ${median(logins)} ms`);
  }
  const failed = results.filter((result) => !result.ok);
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  for (const result of failed) console.log(`  FAIL ${result.id} ${result.name}`);
  process.exitCode = failed.length ? 1 : 0;
}

main().catch((error) => {
  console.error(`ERROR ${error.stack || error.message}`);
  process.exitCode = 2;
});
