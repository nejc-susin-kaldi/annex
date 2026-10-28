"""__APP_NAME__: an Annex web tool (FastAPI)."""
import html
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, gateway, log,
                           report_browser_error, request_context)

app = FastAPI(title="__APP_NAME__")
# Refuses state-changing requests from other sites (the contract's "Cross-site requests"); keep it on. Added before
# request_log so a refusal still gets a request id and an access line.
app.add_middleware(CsrfMiddleware)


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


@app.get("/annex-errors.js")
def annex_errors_js():
    """The script that reports this page's JavaScript errors to /api/_errors (keep both routes)."""
    return Response(browser_errors_js(), media_type="application/javascript")


@app.post("/api/_errors")
async def browser_errors(request: Request):
    """Browser error reports (from /annex-errors.js): logged as one error line, grouped and emailed like server errors."""
    return Response(status_code=report_browser_error(request.headers, await request.body()))


@app.get("/healthz")
def healthz():
    return {"ok": True}


# Plain `def` endpoints run in a thread pool, so blocking gateway calls are fine.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    try:
        rows = gateway("machine_floor", order="games_30d.desc", limit=5)
        items = "".join(f"<li>{html.escape(r['title'])} at {html.escape(r['venue_name'])}: {r['games_30d']:,} games</li>" for r in rows)
        data = f"<h2>Busiest machines (30 days)</h2><ol>{items}</ol>"
    except GatewayError as e:
        log.warn("gateway read failed", view="machine_floor", status=e.status)
        data = f"<p>Gateway: {html.escape(str(e))}</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>__APP_NAME__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1>Hello, {html.escape(user.name)}</h1>{data}<script src="/annex-errors.js"></script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
