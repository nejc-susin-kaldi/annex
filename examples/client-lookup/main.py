"""client-lookup: search clients through the Annex data gateway.

Shows the three things Annex gives every app: the signed-in user (no login
code), scoped product data (only the declared views), and a discovery entry.
"""
import html
import os
import time

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, gateway, log,
                           report_browser_error, request_context)

app = FastAPI(title="client-lookup")
app.add_middleware(CsrfMiddleware)  # refuses state-changing requests from other sites (contract: Cross-site requests)

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

GRANTED = ["client_summary", "top_clients_by_aum"]
NOT_GRANTED = ["monthly_positions", "advisor_book"]

STYLE = """
body{font-family:system-ui,sans-serif;max-width:56rem;margin:2rem auto;padding:0 1rem;color:#1f2933}
h1{margin-bottom:.2rem} .sub{color:#61707d;margin-top:0}
form{margin:1.5rem 0} input{font-size:1rem;padding:.5rem .7rem;width:20rem;max-width:70%}
button{font-size:1rem;padding:.5rem 1rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem} th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #e4e7eb}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:#18794e}.no{color:#b42318} .box{background:#f5f7fa;border-radius:8px;padding:.8rem 1rem}
"""


def eur(v) -> str:
    return f"{float(v):,.0f} €"


def table(rows: list[dict]) -> str:
    if not rows:
        return "<p>No clients found.</p>"
    head = ("<tr><th>Client</th><th>Segment</th><th>Risk</th><th>Country</th><th>Advisor</th>"
            "<th class=num>Accounts</th><th class=num>AUM</th></tr>")
    body = "".join(
        f"<tr><td>{html.escape(r['client_name'])}</td><td>{r['segment']}</td><td>{r['risk_profile']}</td>"
        f"<td>{html.escape(r['country'])}</td><td>{html.escape(r['advisor_name'])}</td>"
        f"<td class=num>{r['account_count']}</td><td class=num>{eur(r['aum_eur'])}</td></tr>" for r in rows)
    return f"<table>{head}{body}</table>"


def access_check() -> str:
    items = []
    for view in GRANTED + NOT_GRANTED:
        try:
            gateway(view, limit=1)
            items.append(f"<li><code>{view}</code>: <span class=ok>allowed</span></li>")
        except GatewayError as e:
            log.debug("gateway view not granted", view=view, status=e.status)
            items.append(f"<li><code>{view}</code>: <span class=no>denied ({e.status})</span></li>")
    return "<ul>" + "".join(items) + "</ul>"


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = Query("", max_length=60)):
    user = current_user(request.headers)
    q = q.strip()
    try:
        if q:
            pattern = "*" + q.replace("*", "").replace(",", " ") + "*"
            rows = gateway("client_summary", client_name=f"ilike.{pattern}", order="aum_eur.desc", limit=50)
            log.info("client search", chars=len(q), matches=len(rows))  # counts, never the search text
            results = f"<h2>Clients matching “{html.escape(q)}”</h2>" + table(rows)
        else:
            top = gateway("top_clients_by_aum", limit=10)
            results = "<h2>Top 10 clients by AUM</h2><table><tr><th>#</th><th>Client</th><th>Segment</th>" \
                      "<th>Advisor</th><th class=num>AUM</th></tr>" + "".join(
                f"<tr><td>{r['rank']}</td><td>{html.escape(r['client_name'])}</td><td>{r['segment']}</td>"
                f"<td>{html.escape(r['advisor_name'])}</td><td class=num>{eur(r['aum_eur'])}</td></tr>" for r in top
            ) + "</table>"
    except GatewayError as e:
        log.error("could not load clients", status=e.status, error=str(e))
        results = f"<p class=no>Could not load data: {html.escape(str(e))}</p>"
    groups = ", ".join(user.groups) or "none"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Client lookup</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{STYLE}</style></head><body>
<h1>Hello, {html.escape(user.name)}</h1>
<p class=sub>Signed in as <b>{html.escape(user.login)}</b> ({html.escape(user.email)}), groups: {html.escape(groups)}</p>
<form method=get><input name=q value="{html.escape(q)}" placeholder="Search clients, e.g. Novak" autofocus>
<button>Search</button></form>
{results}
<div class=box><b>Data access for this app</b> (enforced by the gateway, not by this code):{access_check()}</div>
<script src="/annex-errors.js"></script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
