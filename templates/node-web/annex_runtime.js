import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes, timingSafeEqual } from "node:crypto";

// Helpers for the Annex app contract: identity, logging, the data gateway, email, Slack and incoming webhooks.
// Keep this file as is.

/** The signed-in user, from the headers Annex's front door sets (and strips from clients). */
export function currentUser(req) {
  const login = req.get("remote-user") || "";
  return {
    login,
    name: req.get("remote-name") || login,
    email: req.get("remote-email") || "",
    groups: (req.get("remote-groups") || "").split(",").filter(Boolean),
  };
}


// --- Logging: one JSON object per line on stdout (the Logging section of `annex contract`) ------------------------
// Required: ts, level (debug|info|warn|error), msg. Inside a request every line also gets request_id and user
// (annexRequest below). LOG_LEVEL (injected, default info) sets the threshold. Never log secrets, tokens,
// passwords, request bodies or full product records: IDs and counts are fine.
const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = LEVELS[(process.env.LOG_LEVEL || "info").trim().toLowerCase().replace("warning", "warn")] ?? LEVELS.info;
const requestStore = new AsyncLocalStorage();
const RESERVED = ["ts", "level", "msg", "request_id", "user"];
const REQUEST_ID_RE = /^[A-Za-z0-9._-]{8,64}$/;

function emit(level, msg, fields = {}, context) {
  if (LEVELS[level] < threshold) return;
  const ctx = context || requestStore.getStore();
  const line = { ts: new Date().toISOString(), level, msg: String(msg) };
  if (ctx?.request_id) line.request_id = ctx.request_id;
  if (ctx?.user) line.user = ctx.user;
  for (const [k, v] of Object.entries(fields || {})) line[RESERVED.includes(k) ? `${k}_` : k] = v instanceof Error ? `${v.name}: ${v.message}` : v;
  process.stdout.write(JSON.stringify(line) + "\n");
}

/** log.info("report written", { rows: 12, ms: 340 }). The second argument's keys become fields of the line. */
export const log = {
  debug: (msg, fields) => emit("debug", msg, fields),
  info: (msg, fields) => emit("info", msg, fields),
  warn: (msg, fields) => emit("warn", msg, fields),
  error: (msg, fields) => emit("error", msg, fields),
};

/** 16 random hex characters. */
export function newRequestId() {
  return randomBytes(8).toString("hex");
}

/** Express middleware: reuses an incoming X-Request-Id if it looks sane (8-64 letters, digits, . _ -) or makes one,
 *  returns it as the X-Request-Id response header, puts request_id and user on every log line of the request, and
 *  logs one access line when it ends (method, path without the query string, status, ms). Health checks are logged
 *  at debug level. Use it first: app.use(annexRequest). */
export function annexRequest(req, res, next) {
  const incoming = req.get("x-request-id") || "";
  const request_id = REQUEST_ID_RE.test(incoming) ? incoming : newRequestId();
  const t0 = process.hrtime.bigint();
  res.setHeader("X-Request-Id", request_id);
  const context = { request_id, user: req.get("remote-user") || "" };
  requestStore.run(context, () => {
    // 'finish' may fire outside this async context, so the access line gets it explicitly
    res.on("finish", () => emit(req.path === "/healthz" ? "debug" : "info", "request",
      { method: req.method, path: req.path, status: res.statusCode, ms: Number((process.hrtime.bigint() - t0) / 1000000n) }, context));
    next();
  });
}

export class GatewayError extends Error {
  constructor(status, message) {
    super(`gateway returned ${status}: ${message}`);
    this.status = status;
  }
}

/** Read a gateway view, e.g. gateway("client_summary", { client_name: "ilike.*novak*", limit: 20 }). */
export async function gateway(view, params = {}) {
  const base = process.env.ANNEX_GATEWAY_URL;
  if (!base) throw new GatewayError(0, "no gateway access: add the view under data.views in annex.yaml");
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`${base.replace(/\/$/, "")}/${view}${qs ? "?" + qs : ""}`, {
    headers: { Authorization: `Bearer ${process.env.ANNEX_GATEWAY_TOKEN}`, Accept: "application/json" },
  });
  if (!res.ok) throw new GatewayError(res.status, (await res.text()).slice(0, 300));
  return res.json();
}

/** An email or Slack call failed. .status is the HTTP status (0: annex-ops wasn't reachable or the app didn't
 *  declare notify:). The message is the server's own and says what to fix. */
export class NotifyError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function ops(path, payload) {
  const base = process.env.ANNEX_OPS_URL;
  if (!base) throw new NotifyError(0, "add notify: {email: true} (or slack: [...]) to annex.yaml and deploy again");
  let res;
  try {
    res = await fetch(`${base.replace(/\/$/, "")}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.ANNEX_OPS_TOKEN}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(20000),
    });
  } catch (e) {
    throw new NotifyError(0, `can't reach annex-ops (${e.cause?.code || e.cause?.errors?.[0]?.code || e.message}). Try again in a minute`);
  }
  const body = await res.text();
  if (!res.ok) {
    let message = body.slice(0, 300);
    try {
      message = JSON.parse(body).error || message;
    } catch {}
    throw new NotifyError(res.status, message);
  }
  return JSON.parse(body);
}

/** Send one email through Annex. `to` is a login ("cm"), a company address or an array of them; give exactly one
 *  of text, html or template ("notice" with data {title, body, link, link_text}). No attachments: link to a file
 *  in your app instead. Returns {id, to}. Needs notify: {email: true} in annex.yaml. Throws NotifyError. */
export function sendEmail(to, subject, { text, html, template, data } = {}) {
  return ops("/v1/email", JSON.parse(JSON.stringify({ to, subject, text, html, template, data })));
}

/** Post to a Slack channel the app declared under notify.slack in annex.yaml, e.g. "#finance-reports".
 *  Returns {id, channel, ts}. Throws NotifyError. */
export function postSlack(channel, text, blocks) {
  return ops("/v1/slack", JSON.parse(JSON.stringify({ channel, text, blocks })));
}

/** A webhook delivery from Annex isn't authentic. .status is the HTTP status to answer with (401). */
export class HookError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** Check that a request really comes from Annex (annex-ops, after it verified the provider's signature) and return
 *  { hook, eventId, attempt }. Call it first in every webhook endpoint (declared under webhooks: in annex.yaml);
 *  a HookError (.status 401) means answer 401 and do nothing. Answer 2xx quickly, and skip an eventId you have
 *  seen: deliveries are at least once. */
export function hookEvent(req) {
  const expected = Buffer.from(process.env.ANNEX_HOOK_TOKEN || "");
  const got = Buffer.from(req.get("x-annex-hook-token") || "");
  if (!expected.length || got.length !== expected.length || !timingSafeEqual(got, expected)) {
    throw new HookError(401, "missing or wrong X-Annex-Hook-Token: this endpoint takes webhooks forwarded by Annex only");
  }
  return { hook: req.get("x-annex-hook") || "", eventId: req.get("x-annex-hook-event") || "",
           attempt: parseInt(req.get("x-annex-hook-attempt") || "1", 10) || 1 };
}
