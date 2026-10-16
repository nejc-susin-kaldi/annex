"""monthly-report: the latest report, a CSV download and the run history."""
import csv
import html
import os
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from annex_runtime import current_user, log, request_context
from store import REPORTS, connect

app = FastAPI(title="monthly-report")

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

TITLE = os.environ.get("REPORT_TITLE", "Monthly report")
STYLE = """
body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1f2933}
.sub{color:#61707d;margin-top:0} a.btn{display:inline-block;padding:.45rem .9rem;background:#1f6feb;color:#fff;
border-radius:6px;text-decoration:none} table{border-collapse:collapse;width:100%;margin:.6rem 0 1.6rem}
th,td{text-align:left;padding:.3rem .55rem;border-bottom:1px solid #e4e7eb;font-size:.92rem}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
"""


def eur(v) -> str:
    return f"{float(v):,.0f}"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/download/{name}")
def download(name: str):
    path = REPORTS / name
    if "/" in name or not name.endswith(".csv") or not path.is_file():
        log.warn("report not found", file=name)
        raise HTTPException(404, "no such report")
    log.info("report downloaded", file=name)
    return FileResponse(path, media_type="text/csv", filename=name)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    with connect() as conn:
        runs = conn.execute("SELECT * FROM report_runs ORDER BY ran_at DESC LIMIT 15").fetchall()
    body = [f"<h1>{html.escape(TITLE)}</h1><p class=sub>Hello, {html.escape(user.name)}.</p>"]
    if not runs:
        body.append("<p>No report yet. It runs at 06:00 on the 1st of each month (and every 10 minutes for the demo), "
                    "or now with <code>annex run monthly-report monthly</code>.</p>")
    else:
        last = runs[0]
        path = REPORTS / last["file"]
        body.append(f"<p>Latest: <b>{last['month_end']:%B %Y}</b>, {last['clients']} clients, total "
                    f"<b>{eur(last['total_value_eur'])} EUR</b>, generated {last['ran_at']:%Y-%m-%d %H:%M %Z}. "
                    f"<a class=btn href='download/{html.escape(last['file'])}'>Download CSV</a></p>")
        if path.is_file():
            with path.open() as f:
                rows = list(csv.DictReader(f))[:15]
            cols = [c for c in rows[0] if c not in ("client_id",)] if rows else []
            body.append("<h2>Top 15 clients</h2><table><tr>" + "".join(
                f"<th class={'n' if c != 'client_name' else ''}>{html.escape(c.replace('_', ' '))}</th>" for c in cols) + "</tr>")
            body += ["<tr>" + "".join(f"<td>{html.escape(r[c])}</td>" if c == "client_name" else
                                      f"<td class=n>{eur(r[c])}</td>" for c in cols) + "</tr>" for r in rows]
            body.append("</table>")
        body.append("<h2>Run history</h2><table><tr><th>Ran at</th><th>Month</th><th class=n>Clients</th>"
                    "<th class=n>Positions</th><th class=n>Total EUR</th><th class=n>ms</th><th>File</th></tr>")
        body += [f"<tr><td>{r['ran_at']:%Y-%m-%d %H:%M:%S}</td><td>{r['month_end']:%Y-%m}</td><td class=n>{r['clients']}</td>"
                 f"<td class=n>{r['positions']}</td><td class=n>{eur(r['total_value_eur'])}</td><td class=n>{r['duration_ms']}</td>"
                 f"<td><a href='download/{html.escape(r['file'])}'>{html.escape(r['file'])}</a></td></tr>" for r in runs]
        body.append("</table>")
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(TITLE)}</title>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'><style>{STYLE}</style></head>"
            f"<body>{''.join(body)}</body></html>")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
