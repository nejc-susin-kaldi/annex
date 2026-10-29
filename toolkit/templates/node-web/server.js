// __APP_NAME__: an Annex web tool (Express).
import express from "express";
import { annexRequest, csrfGuard, currentUser, gateway, GatewayError, log, mountBrowserErrors } from "./annex_runtime.js";

const app = express();
// First: a request id on every log line and in the X-Request-Id response header, plus one access line per request.
app.use(annexRequest);
// Refuses state-changing requests from other sites (the contract's "Cross-site requests"); keep it on.
app.use(csrfGuard);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

app.get("/healthz", (req, res) => res.json({ ok: true }));
// Reports this page's JavaScript errors (the <script src="/annex-errors.js"> below) as logged errors: keep it.
mountBrowserErrors(app, express);

// To read a data view, list it under data.views in annex.yaml (`annex contract` names this instance's views), then:
//     const rows = await gateway("<view>", { order: "day.desc", limit: 20 });  // GatewayError when it fails: log.warn it
app.get("/", async (req, res) => {
  const user = currentUser(req);
  const data = "<p>This is the start of __APP_NAME__.</p>";
  res.send(`<!doctype html><html><head><meta charset="utf-8"><title>__APP_NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1>Hello, ${esc(user.name)}</h1>${data}<script src="/annex-errors.js"></script></body></html>`);
});

// Last: anything unhandled becomes one error line with its stack, and a plain 500.
app.use((err, req, res, next) => {
  log.error("unhandled error", { method: req.method, path: req.path, exc: `${err.name}: ${err.message}`, stack: err.stack });
  res.status(500).send("Internal Server Error");
});

const port = Number(process.env.PORT || 8080);
app.listen(port, "0.0.0.0", () => log.info("started", { port }));
