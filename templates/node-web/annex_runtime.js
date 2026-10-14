// Helpers for the Annex app contract: identity and the data gateway. Keep this file as is.

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
