// The MCP client side: an in-memory OAuthClientProvider for the SDK, a fetch
// that records every request the SDK makes, and signIn(), which runs the
// whole authorization flow the way an interactive MCP client does.
import crypto from "node:crypto";
import {
  Client,
  StreamableHTTPClientTransport,
  UnauthorizedError,
  auth,
} from "@modelcontextprotocol/client";

import { Browser } from "./browser.mjs";

export const CLIENT_INFO = { name: "o3p-mcp-e2e", version: "1.0.0" };

export class MemoryOAuthProvider {
  constructor({ redirectUrl, clientMetadata = {}, clientInformation }) {
    this._redirectUrl = redirectUrl;
    // A client registered earlier (the SDK then skips registration).
    this._client = clientInformation;
    this._metadata = {
      client_name: "o3p-mcp e2e client",
      redirect_uris: [redirectUrl],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
      ...clientMetadata,
    };
    this._state = crypto.randomBytes(16).toString("base64url");
    this.savedTokens = [];
    this.invalidated = [];
  }
  get redirectUrl() {
    return this._redirectUrl;
  }
  get clientMetadata() {
    return this._metadata;
  }
  state() {
    return this._state;
  }
  clientInformation() {
    return this._client;
  }
  saveClientInformation(info) {
    this._client = info;
  }
  tokens() {
    return this._tokens;
  }
  saveTokens(tokens) {
    this._tokens = tokens;
    this.savedTokens.push(tokens);
  }
  redirectToAuthorization(url) {
    this.authorizationUrl = new URL(url);
  }
  saveCodeVerifier(verifier) {
    this._verifier = verifier;
  }
  codeVerifier() {
    return this._verifier;
  }
  saveDiscoveryState(state) {
    this._discovery = state;
  }
  discoveryState() {
    return this._discovery;
  }
  invalidateCredentials(scope) {
    this.invalidated.push(scope);
    if (scope === "all" || scope === "tokens") this._tokens = undefined;
    if (scope === "all" || scope === "client") this._client = undefined;
    if (scope === "all" || scope === "verifier") this._verifier = undefined;
    if (scope === "all" || scope === "discovery") this._discovery = undefined;
  }
}

// A fetch that records method, URL, status and the interesting headers of
// every request; bodies are left untouched.
export function recordingFetch() {
  const entries = [];
  const fetchFn = async (input, init = {}) => {
    const request = new Request(input, init);
    const started = performance.now();
    const response = await fetch(input, init);
    entries.push({
      method: request.method,
      url: request.url,
      status: response.status,
      ms: performance.now() - started,
      authorization: request.headers.get("authorization") ? request.headers.get("authorization").split(" ")[0] : null,
      wwwAuthenticate: response.headers.get("www-authenticate"),
    });
    return response;
  };
  return { fetch: fetchFn, entries };
}

export async function connect(mcpUrl, provider, fetchFn) {
  const transport = new StreamableHTTPClientTransport(new URL(mcpUrl), {
    authProvider: provider,
    ...(fetchFn ? { fetch: fetchFn } : {}),
  });
  const client = new Client(CLIENT_INFO);
  await client.connect(transport);
  return { client, transport };
}

// Connects with a fixed bearer token (no OAuth provider): what a client does
// with a token it already holds.
export async function connectWithToken(mcpUrl, accessToken) {
  const transport = new StreamableHTTPClientTransport(new URL(mcpUrl), {
    authProvider: { token: async () => accessToken },
  });
  const client = new Client(CLIENT_INFO);
  await client.connect(transport);
  return { client, transport };
}

// The whole sign-in of an interactive MCP client:
//   1. connect without a token → 401 → the SDK discovers the protected
//      resource and the authorization server, registers, and asks to
//      redirect the user (or, with `scope`, auth() is called with it);
//   2. the user agent opens the authorization URL, signs in, consents;
//   3. the redirect's code (and iss) is exchanged by the SDK;
//   4. a new connection with the token.
// Returns { outcome, client?, provider, trace, browser, authorization, timings }.
export async function signIn(options) {
  const {
    mcpUrl,
    issuer,
    login,
    password,
    scope,
    decision = "approve",
    redirectUrl = "http://127.0.0.1:18999/callback",
    clientMetadata,
    clientInformation,
    browser = new Browser(issuer),
  } = options;
  const recorder = recordingFetch();
  const provider = new MemoryOAuthProvider({ redirectUrl, clientMetadata, clientInformation });
  const timings = {};
  const started = performance.now();
  if (scope) {
    const result = await auth(provider, { serverUrl: mcpUrl, scope, fetchFn: recorder.fetch });
    if (result !== "REDIRECT") throw new Error(`auth() answered ${result}, expected REDIRECT`);
  } else {
    const transport = new StreamableHTTPClientTransport(new URL(mcpUrl), {
      authProvider: provider,
      fetch: recorder.fetch,
    });
    const client = new Client(CLIENT_INFO);
    try {
      await client.connect(transport);
      throw new Error("connected without signing in");
    } catch (error) {
      if (!(error instanceof UnauthorizedError)) throw error;
    }
  }
  timings.discoveryMs = performance.now() - started;
  if (!provider.authorizationUrl) throw new Error("the SDK did not ask to redirect the user");
  const authorization = await browser.authorize(provider.authorizationUrl, {
    login,
    password,
    decision,
  });
  timings.browser = authorization.timings;
  const result = { provider, trace: recorder.entries, browser, authorization, timings };
  if (authorization.outcome !== "redirect") return { ...result, outcome: authorization.outcome };
  const callback = new URL(authorization.callback);
  result.callback = callback;
  if (callback.searchParams.get("state") !== provider.state()) {
    throw new Error("the callback state does not match");
  }
  if (!callback.searchParams.get("code")) return { ...result, outcome: "error-redirect" };
  const exchangeStart = performance.now();
  const exchanged = await auth(provider, {
    serverUrl: mcpUrl,
    authorizationCode: callback.searchParams.get("code"),
    iss: callback.searchParams.get("iss") ?? undefined,
    fetchFn: recorder.fetch,
  });
  if (exchanged !== "AUTHORIZED") throw new Error(`code exchange answered ${exchanged}`);
  timings.exchangeMs = performance.now() - exchangeStart;
  const connectStart = performance.now();
  const { client, transport } = await connect(mcpUrl, provider, recorder.fetch);
  timings.connectMs = performance.now() - connectStart;
  timings.totalMs = performance.now() - started;
  return { ...result, outcome: "signed-in", client, transport, tokens: provider.tokens() };
}

export function toolNames(listing) {
  return listing.tools.map((tool) => tool.name).sort();
}

// The parsed JSON of a tool result's first text block, or the text itself.
export function toolJson(result) {
  const text = result.content?.find((block) => block.type === "text")?.text;
  if (result.structuredContent) return result.structuredContent;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
