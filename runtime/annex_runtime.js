import { timingSafeEqual } from "node:crypto";

// Helpers for the Annex app contract: identity, the data gateway, email, Slack and incoming webhooks. Keep this file as is.

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
