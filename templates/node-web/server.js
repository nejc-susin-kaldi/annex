// __APP_NAME__: an Annex web tool (Express).
import express from "express";
import { currentUser, gateway, GatewayError } from "./annex_runtime.js";

const app = express();
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

app.get("/healthz", (req, res) => res.json({ ok: true }));

app.get("/", async (req, res) => {
  const user = currentUser(req);
  let data;
  try {
    const rows = await gateway("top_clients_by_aum", { limit: 5 });
    data = "<h2>Top clients</h2><ol>" +
      rows.map((r) => `<li>${esc(r.client_name)}: ${Math.round(r.aum_eur).toLocaleString()} EUR</li>`).join("") + "</ol>";
  } catch (e) {
    data = `<p>Gateway: ${esc(e instanceof GatewayError ? e.message : "unavailable")}</p>`;
  }
  res.send(`<!doctype html><html><head><meta charset="utf-8"><title>__APP_NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1>Hello, ${esc(user.name)}</h1>${data}</body></html>`);
});

const port = Number(process.env.PORT || 8080);
app.listen(port, "0.0.0.0", () => console.log(`listening on :${port}`));
