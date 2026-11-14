// Odoo as the test sees it: an administrator session that calls model
// methods the way the web client does (JSON-RPC with the session cookie),
// and raw OAuth requests for the checks that need exact control.
import crypto from "node:crypto";

import { Browser } from "./browser.mjs";

export class OdooSession {
  constructor(origin) {
    this.browser = new Browser(origin);
  }

  async login(login, password) {
    const page = await this.browser.navigate("/web/login");
    page.url = new URL("/web/login", this.browser.origin).href;
    await this.browser.login(page, login, password);
    return this;
  }

  async rpc(path, params) {
    const answer = await this.browser.request(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", method: "call", id: 1, params }),
    });
    const payload = JSON.parse(answer.html);
    if (payload.error) {
      const error = new Error(payload.error.data?.message || payload.error.message);
      error.name = payload.error.data?.name || "RpcError";
      throw error;
    }
    return payload.result;
  }

  callKw(model, method, args = [], kwargs = {}) {
    return this.rpc(`/web/dataset/call_kw/${model}/${method}`, { model, method, args, kwargs });
  }

  callButton(model, method, ids) {
    return this.rpc(`/web/dataset/call_button/${model}/${method}`, {
      model,
      method,
      args: [ids],
      kwargs: {},
    });
  }

  // Saves settings the way the Settings form does: a new record, then execute.
  async saveSettings(values) {
    const id = await this.callKw("res.config.settings", "create", [values]);
    await this.callButton("res.config.settings", "execute", [id]);
    return id;
  }
}

export function pkce() {
  const verifier = crypto.randomBytes(48).toString("base64url");
  const challenge = crypto.createHash("sha256").update(verifier).digest("base64url");
  return { verifier, challenge };
}

export function randomToken() {
  return crypto.randomBytes(32).toString("base64url");
}

export class OAuth {
  constructor(issuer, introspection) {
    this.issuer = issuer;
    this.introspection = introspection;
  }

  async metadata() {
    return (await fetch(`${this.issuer}/.well-known/oauth-authorization-server`)).json();
  }

  async register(body) {
    const response = await fetch(`${this.issuer}/o3p_mcp/oauth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json(), headers: response.headers };
  }

  authorizeUrl(params) {
    const url = new URL(`${this.issuer}/o3p_mcp/oauth/authorize`);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, value);
    }
    return url;
  }

  // POST a form to an endpoint with the client's authentication method.
  async post(path, params, client) {
    const headers = { "content-type": "application/x-www-form-urlencoded" };
    const form = new URLSearchParams(params);
    const method = client?.token_endpoint_auth_method || "none";
    if (client && method === "client_secret_basic") {
      const encode = (value) => encodeURIComponent(value);
      headers.authorization = `Basic ${Buffer.from(
        `${encode(client.client_id)}:${encode(client.client_secret)}`,
      ).toString("base64")}`;
    } else if (client) {
      form.set("client_id", client.client_id);
      if (method === "client_secret_post") form.set("client_secret", client.client_secret);
    }
    const response = await fetch(`${this.issuer}${path}`, { method: "POST", headers, body: form });
    const text = await response.text();
    let body = text;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      // Not JSON: kept as text.
    }
    return { status: response.status, body, headers: response.headers };
  }

  token(params, client) {
    return this.post("/o3p_mcp/oauth/token", params, client);
  }

  refresh(refreshToken, client, extra = {}) {
    return this.token({ grant_type: "refresh_token", refresh_token: refreshToken, ...extra }, client);
  }

  revoke(token, client, hint) {
    return this.post(
      "/o3p_mcp/oauth/revoke",
      { token, ...(hint ? { token_type_hint: hint } : {}) },
      client,
    );
  }

  // Odoo's own view of a token, as the resource server sees it.
  async introspect(token) {
    const { client_id: id, client_secret: secret } = this.introspection;
    const response = await fetch(`${this.issuer}/o3p_mcp/oauth/introspect`, {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        authorization: `Basic ${Buffer.from(`${id}:${secret}`).toString("base64")}`,
      },
      body: new URLSearchParams({ token }),
    });
    return response.json();
  }

  // The authorization code flow without the SDK: register (unless `client`
  // is given), authorize through `browser`, exchange the code.
  async codeFlow({ browser, login, password, client, scope, resource, redirectUri, exchange = {} }) {
    let registered = client;
    if (!registered) {
      const answer = await this.register({
        client_name: "o3p-mcp e2e raw client",
        redirect_uris: [redirectUri],
        grant_types: ["authorization_code", "refresh_token"],
        token_endpoint_auth_method: "none",
      });
      if (answer.status !== 201) throw new Error(`registration answered ${answer.status}`);
      registered = answer.body;
    }
    const { verifier, challenge } = pkce();
    const state = randomToken();
    const target = redirectUri || registered.redirect_uris[0];
    const authorization = await browser.authorize(
      this.authorizeUrl({
        response_type: "code",
        client_id: registered.client_id,
        redirect_uri: target,
        scope,
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
        resource,
      }),
      { login, password },
    );
    const result = { client: registered, authorization, state, verifier };
    if (authorization.outcome !== "redirect") return result;
    const callback = new URL(authorization.callback);
    result.callback = callback;
    const code = callback.searchParams.get("code");
    if (!code) return result;
    result.code = code;
    result.exchange = await this.token(
      {
        grant_type: "authorization_code",
        code,
        code_verifier: verifier,
        redirect_uri: target,
        ...exchange,
      },
      registered,
    );
    return result;
  }
}
