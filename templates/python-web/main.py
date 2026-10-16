"""__APP_NAME__: an Annex web tool (FastAPI)."""
import html
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from annex_runtime import GatewayError, current_user, gateway, log, request_context

app = FastAPI(title="__APP_NAME__")


@app.middleware("http")
async def request_log(request: Request, call_next):
    """One request id per request: on every log line, in the X-Request-Id response header and in Annex's access
    log, so one id finds everything about a request. Plus one access line per request (health checks at debug)."""
    t0 = time.monotonic()
    with request_context(request.headers) as request_id:
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", method=request.method, path=request.url.path)
            response = PlainTextResponse("Internal Server Error", status_code=500)
        response.headers["X-Request-Id"] = request_id
        (log.debug if request.url.path == "/healthz" else log.info)(
            "request", method=request.method, path=request.url.path, status=response.status_code,
            ms=round((time.monotonic() - t0) * 1000))
        return response


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
        log.warn("gateway read failed", view="top_clients_by_aum", status=e.status)
        data = f"<p>Gateway: {html.escape(str(e))}</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>__APP_NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1>Hello, {html.escape(user.name)}</h1>{data}</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
