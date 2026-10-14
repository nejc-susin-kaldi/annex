"""client-lookup: search clients through the Annex data gateway.

Shows the three things Annex gives every app: the signed-in user (no login
code), scoped product data (only the declared views), and a discovery entry.
"""
import html
import os

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse

from annex_runtime import GatewayError, current_user, gateway

app = FastAPI(title="client-lookup")
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
            items.append(f"<li><code>{view}</code>: <span class=no>denied ({e.status})</span></li>")
    return "<ul>" + "".join(items) + "</ul>"


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
            results = f"<h2>Clients matching “{html.escape(q)}”</h2>" + table(rows)
        else:
            top = gateway("top_clients_by_aum", limit=10)
            results = "<h2>Top 10 clients by AUM</h2><table><tr><th>#</th><th>Client</th><th>Segment</th>" \
                      "<th>Advisor</th><th class=num>AUM</th></tr>" + "".join(
                f"<tr><td>{r['rank']}</td><td>{html.escape(r['client_name'])}</td><td>{r['segment']}</td>"
                f"<td>{html.escape(r['advisor_name'])}</td><td class=num>{eur(r['aum_eur'])}</td></tr>" for r in top
            ) + "</table>"
    except GatewayError as e:
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
</body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True,
                forwarded_allow_ips="*")
