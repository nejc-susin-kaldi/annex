"""__APP_NAME__: an Annex web tool (FastAPI)."""
import html
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from annex_runtime import GatewayError, current_user, gateway

app = FastAPI(title="__APP_NAME__")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# Plain `def` endpoints run in a thread pool, so blocking gateway calls are fine.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    try:
        rows = gateway("top_clients_by_aum", limit=5)
        items = "".join(f"<li>{html.escape(r['client_name'])}: {r['aum_eur']:,.0f} EUR</li>" for r in rows)
        data = f"<h2>Top clients</h2><ol>{items}</ol>"
    except GatewayError as e:
        data = f"<p>Gateway: {html.escape(str(e))}</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>__APP_NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1>Hello, {html.escape(user.name)}</h1>{data}</body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True,
                forwarded_allow_ips="*")
