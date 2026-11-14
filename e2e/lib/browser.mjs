// A minimal user agent for Odoo: a cookie jar, manual redirects, the
// /web/login form and the o3p_mcp consent form. It never follows a redirect
// that leaves Odoo, so a redirect to the MCP client's redirect URI is
// returned to the caller exactly as a browser would receive it.

const NAMED_ENTITIES = { quot: '"', apos: "'", lt: "<", gt: ">", amp: "&", nbsp: "\u00a0" };

// Decodes HTML character references in one pass (named, decimal, hex).
const decodeEntities = (value) =>
  value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, entity) => {
    if (entity[0] !== "#") return NAMED_ENTITIES[entity] ?? match;
    const hex = entity[1].toLowerCase() === "x";
    return String.fromCodePoint(Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10));
  });

// The value of attribute `name` in one HTML tag.
function attribute(tag, name) {
  const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
  return match ? decodeEntities(match[1]) : undefined;
}

// The <form> elements of a page with their action and named inputs.
export function parseForms(html) {
  const forms = [];
  for (const match of html.matchAll(/<form\b([^>]*)>([\s\S]*?)<\/form>/g)) {
    const inputs = [];
    for (const input of match[2].matchAll(/<(?:input|button)\b[^>]*>/g)) {
      const name = attribute(input[0], "name");
      if (name) inputs.push([name, attribute(input[0], "value") ?? ""]);
    }
    forms.push({
      action: attribute(`<form${match[1]}>`, "action"),
      method: (attribute(`<form${match[1]}>`, "method") || "get").toLowerCase(),
      inputs,
    });
  }
  return forms;
}

export class Browser {
  constructor(origin, options = {}) {
    this.origin = origin;
    this.cookies = new Map();
    this.log = options.log || (() => {});
    this.trace = [];
  }

  cookieHeader() {
    return [...this.cookies].map(([name, value]) => `${name}=${value}`).join("; ");
  }

  storeCookies(response) {
    for (const line of response.headers.getSetCookie?.() || []) {
      const [pair, ...attributes] = line.split(";");
      const equals = pair.indexOf("=");
      const name = pair.slice(0, equals).trim();
      const value = pair.slice(equals + 1).trim();
      const expired = attributes.some((part) => {
        const [key, val] = part.trim().split("=");
        return (
          (key.toLowerCase() === "max-age" && Number(val) <= 0) ||
          (key.toLowerCase() === "expires" && Date.parse(val) < Date.now())
        );
      });
      if (expired || !value) this.cookies.delete(name);
      else this.cookies.set(name, value);
    }
  }

  // One request, no redirect followed. Returns { status, location, html, response }.
  async request(url, init = {}) {
    const target = new URL(url, this.origin);
    const headers = new Headers(init.headers || {});
    if (target.origin === new URL(this.origin).origin && this.cookies.size) {
      headers.set("cookie", this.cookieHeader());
    }
    headers.set("user-agent", "o3p-mcp-e2e-browser/1.0");
    const response = await fetch(target, { ...init, headers, redirect: "manual" });
    this.storeCookies(response);
    const location = response.headers.get("location");
    const html = await response.text();
    this.trace.push({ method: init.method || "GET", url: target.href, status: response.status, location });
    return {
      status: response.status,
      location: location ? new URL(location, target).href : null,
      html,
      response,
    };
  }

  // Follows redirects while they stay on Odoo; stops at the first one that
  // leaves it (returned with `left: true`) or at a non-redirect answer.
  async navigate(url, init = {}) {
    let answer = await this.request(url, init);
    for (let hops = 0; hops < 10 && answer.location; hops += 1) {
      if (new URL(answer.location).origin !== new URL(this.origin).origin) {
        return { ...answer, left: true };
      }
      answer = await this.request(answer.location);
    }
    return { ...answer, left: false };
  }

  async postForm(action, fields, base) {
    const body = new URLSearchParams(fields);
    return this.request(new URL(action, base || this.origin), {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", origin: this.origin },
      body,
    });
  }

  // Signs in through /web/login when Odoo asks for it. `page` is an answer
  // whose URL is the login page. Returns the page Odoo lands on.
  async login(page, login, password) {
    const form = parseForms(page.html).find((candidate) =>
      candidate.inputs.some(([name]) => name === "password"),
    );
    if (!form) throw new Error("no login form on the page");
    const fields = Object.fromEntries(form.inputs.filter(([name]) => name !== "webauthn_response"));
    fields.login = login;
    fields.password = password;
    const loginUrl = new URL(page.url);
    fields.redirect = loginUrl.searchParams.get("redirect") || fields.redirect || "";
    const answer = await this.postForm(form.action || "/web/login", fields, page.url);
    if (!answer.location) {
      const alert = answer.html.match(/alert-danger[^>]*>([\s\S]*?)<\//);
      throw new Error(`login failed (${answer.status}): ${alert ? alert[1].trim() : "no redirect"}`);
    }
    return this.navigate(answer.location);
  }

  // Runs the user-agent part of an authorization request: open the
  // authorization URL, sign in if asked, then approve or deny on the consent
  // page. Returns { outcome, callback?, consent?, page, timings } where
  // outcome is "redirect" (Odoo sent the browser to `callback`), "consent"
  // (decision "none": stopped on the consent page) or "page" (Odoo answered
  // with a page and no redirect, such as an error page).
  async authorize(authorizationUrl, { login, password, decision = "approve" } = {}) {
    const timings = {};
    const started = performance.now();
    let page = await this.navigate(authorizationUrl);
    page.url = this.trace.at(-1).url;
    if (page.left) return { outcome: "redirect", callback: page.location, page, timings };
    if (new URL(page.url).pathname === "/web/login") {
      const loginStart = performance.now();
      page = await this.login(page, login, password);
      page.url = this.trace.at(-1).url;
      timings.loginMs = performance.now() - loginStart;
      if (page.left) return { outcome: "redirect", callback: page.location, page, timings };
    }
    timings.toConsentMs = performance.now() - started;
    const form = parseForms(page.html).find((candidate) =>
      candidate.inputs.some(([name, value]) => name === "decision" && value === "approve"),
    );
    if (!form) return { outcome: "page", page, timings };
    const consent = { html: page.html, status: page.status, form, headers: page.response.headers };
    if (decision === "none") return { outcome: "consent", consent, page, timings };
    const fields = form.inputs.filter(([name]) => name !== "decision");
    fields.push(["decision", decision]);
    const decisionStart = performance.now();
    const answer = await this.postForm(form.action, fields, page.url);
    timings.decisionMs = performance.now() - decisionStart;
    timings.totalMs = performance.now() - started;
    if (answer.location && new URL(answer.location).origin !== new URL(this.origin).origin) {
      return { outcome: "redirect", callback: answer.location, consent, page: answer, timings };
    }
    return { outcome: "page", consent, page: answer, timings };
  }

  // A CSRF token for the signed-in session, read from any Odoo page that
  // carries one (the web client for internal users, /my for portal users).
  async csrfToken() {
    for (const url of ["/odoo", "/my/account", "/my", "/web"]) {
      const page = await this.navigate(url);
      const match =
        page.html.match(/name="csrf_token"\s+value="([^"]+)"/) ||
        page.html.match(/csrf_token["']?\s*[:=]\s*["']([^"']+)["']/);
      if (match) return match[1];
    }
    return null;
  }
}

export function visibleText(html) {
  return decodeEntities(
    html
      .replace(/<script[\s\S]*?<\/script>/g, " ")
      .replace(/<style[\s\S]*?<\/style>/g, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " "),
  ).trim();
}
