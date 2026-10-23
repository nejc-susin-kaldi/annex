"""weekly-takings: last complete week's takings per venue, the 8-week trend, and the Monday job's run history."""
import csv
import html
import os
import re
import time
from datetime import date

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

import store
from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, log, report_browser_error,
                           request_context)
from takings import TREND_WEEKS, load

app = FastAPI(title="weekly-takings")
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


# --- presentation -------------------------------------------------------------------------------------------------
STYLE = """
:root{--ink:#1d2330;--muted:#647083;--line:#e3e6ec;--bg:#f6f7f9;--card:#fff;--accent:#c2410c;--up:#15803d;--down:#b91c1c}
*{box-sizing:border-box} body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
header{background:#141a26;color:#fff;padding:.9rem 0;border-bottom:3px solid var(--accent)}
.wrap{max-width:68rem;margin:0 auto;padding:0 1rem}
header .wrap{display:flex;align-items:center;gap:.7rem} header b{font-size:1.1rem;letter-spacing:.01em}
header span{color:#aab3c2;font-size:.9rem} header .who{margin-left:auto}
.ball{width:18px;height:18px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#b7bfcc 45%,#5b6474)}
main{padding:1.4rem 0 3rem} h1{font-size:1.45rem;margin:.2rem 0} h2{font-size:1.1rem;margin:2rem 0 .6rem}
.sub{color:var(--muted);margin:.1rem 0 1rem} a{color:#1d4ed8}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.8rem;margin:1rem 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem 1rem}
.tile .k{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.tile .v{font-size:1.5rem;font-weight:650;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow-x:auto}
table{border-collapse:collapse;width:100%} th,td{padding:.45rem .7rem;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;background:#fafbfc}
tr:last-child td{border-bottom:0} td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.total td{font-weight:650;background:#fafbfc} .up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--muted)}
.city{color:var(--muted);font-size:.85rem} .spark{display:block}
.note{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:.7rem 1rem;margin-top:2rem;font-size:.92rem}
.btn{display:inline-block;padding:.4rem .85rem;background:var(--accent);color:#fff;border-radius:6px;text-decoration:none;font-weight:600}
code{background:#eef0f4;padding:.1rem .35rem;border-radius:4px;font-size:.88em} .muted{color:var(--muted)}
"""


def esc(x) -> str:
    return html.escape(str(x))


def eur(x) -> str:
    return "" if x is None else f"{float(x):,.0f}"


def delta(change, pct) -> str:
    """'+1,234 · +5.2%' coloured by direction; 'new' when there was no week before."""
    if change is None:
        return "<td class='n flat'>new</td><td class='n flat'></td>"
    cls = "up" if change > 0.5 else "down" if change < -0.5 else "flat"
    p = "" if pct is None else f"{pct:+.1f}%"
    return f"<td class='n {cls}'>{change:+,.0f}</td><td class='n {cls}'>{p}</td>"


def sparkline(values: list, label: str, w: int = 120, h: int = 28) -> str:
    """A tiny inline SVG line of the weekly amounts, oldest left; gaps where a venue had no takings."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    step = w / (len(values) - 1)
    coords = [(i * step, h - 3 - (v - lo) / span * (h - 6)) for i, v in enumerate(values) if v is not None]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    lx, ly = coords[-1]
    return (f"<svg class=spark width={w} height={h} viewBox='-3 0 {w + 6} {h}' role=img aria-label='{esc(label)}'>"
            f"<title>{esc(label)}</title><polyline points='{path}' fill=none stroke='#64748b' stroke-width=1.5 "
            f"stroke-linejoin=round /><circle cx={lx:.1f} cy={ly:.1f} r=2.6 fill='#c2410c' /></svg>")


def page(title: str, user, body: str) -> str:
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><title>{esc(title)}</title>
<meta name=viewport content="width=device-width, initial-scale=1"><style>{STYLE}</style></head><body>
<header><div class=wrap><div class=ball></div><b>Goat Arcade</b><span>Weekly takings</span>
<span class=who>{esc(user.name)}</span></div></header>
<main class=wrap>{body}</main><script src="/annex-errors.js"></script></body></html>"""


def fmt_week(d: date) -> str:
    return f"{d:%d %b %Y}"


def history_table(runs: list[dict]) -> str:
    if not runs:
        return ("<p class=muted>No runs yet. The job runs every Monday at 07:00 and writes the week's CSV here.</p>")
    rows = []
    for r in runs:
        prev = r["prev_total_eur"]
        change = None if prev is None else float(r["total_eur"]) - float(prev)
        pct = None if not prev else change / float(prev) * 100
        exists = (store.FILES / r["file"]).is_file()
        rows.append(
            f"<tr><td><a href='runs/{r['week_start']:%Y-%m-%d}'>{fmt_week(r['week_start'])}</a></td>"
            f"<td class=n>{eur(r['total_eur'])}</td>{delta(change, pct)}<td class=n>{r['venues']}</td>"
            f"<td>{r['ran_local']:%Y-%m-%d %H:%M}</td><td class=n>{r['runs']}</td>"
            f"<td>{esc(r['emailed_to']) or '<span class=muted>not sent</span>'}</td>"
            f"<td>{f'''<a href='download/{esc(r['file'])}'>CSV</a>''' if exists else '<span class=muted>missing</span>'}</td></tr>")
    return ("<div class=card><table><tr><th>Week of</th><th class=n>Total EUR</th><th class=n>Change</th><th class=n>%</th>"
            "<th class=n>Venues</th><th>Last run</th><th class=n>Runs</th><th>Emailed to</th><th>File</th></tr>"
            + "".join(rows) + "</table></div>")


def builder_note(user) -> str:
    if {"builders", "admins"} & set(user.groups):
        return ("<div class=note><b>For builders:</b> the job runs every Monday at 07:00. "
                "<code>annex run weekly-takings weekly</code> runs it now for the last complete week; running it again "
                "replaces that week's CSV and run instead of adding another. Recipients are "
                f"<code>TAKINGS_RECIPIENTS</code> in annex.yaml (now: {esc(os.environ.get('TAKINGS_RECIPIENTS', 'petra'))}).</div>")
    return ("<div class=note>The summary is emailed every Monday at 07:00 with a link to the week's page. "
            "Need it re-run? Ask Sam (a builder can run it now with <code>annex run weekly-takings weekly</code>).</div>")


# --- pages --------------------------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    try:
        week = load()
    except GatewayError as e:
        log.error("gateway read failed", view="revenue_weekly", status=e.status, error=str(e))
        return HTMLResponse(page("Weekly takings", user, "<h1>Weekly takings</h1><p>The data isn't available "
                                 "right now. Try again in a minute.</p>"), status_code=502)
    except LookupError as e:
        log.warn("no complete week", error=str(e))
        return HTMLResponse(page("Weekly takings", user, "<h1>Weekly takings</h1><p>No complete week in the data yet.</p>"))
    runs = store.history()
    prev = week.prev_total
    in_progress = (f" The week of {fmt_week(week.in_progress)} is still in progress and isn't counted yet."
                   if week.in_progress else "")
    tiles = (f"<div class=tiles><div class=tile><div class=k>Chain total</div><div class=v>{eur(week.total)} EUR</div></div>"
             f"<div class=tile><div class=k>Week before</div><div class=v>{f'{eur(prev)} EUR' if prev else 'n/a'}</div></div>"
             f"<div class=tile><div class=k>Change</div><div class=v>"
             + ("n/a" if week.change_pct is None else
                f"<span class={'up' if week.change_pct >= 0 else 'down'}>{week.change_pct:+.1f}%</span>")
             + f"</div></div><div class=tile><div class=k>{TREND_WEEKS}-week trend</div>"
             f"{sparkline(week.chain_trend, f'Chain total, last {TREND_WEEKS} weeks', 160, 40)}</div></div>")
    head = ("<tr><th>Venue</th><th class=n>Takings EUR</th><th class=n>Change</th><th class=n>%</th>"
            "<th class=n>Coins EUR</th><th class=n>Tokens</th><th class=n>Card plays</th><th class=n>Machines</th>"
            f"<th>Last {TREND_WEEKS} weeks</th></tr>")
    rows = [f"<tr><td>{esc(v.venue_name)} <span class=city>{esc(v.venue_code)} · {esc(v.city)}</span></td>"
            f"<td class=n>{eur(v.amount)}</td>{delta(v.change, v.change_pct)}<td class=n>{eur(v.coins)}</td>"
            f"<td class=n>{v.tokens:,}</td><td class=n>{v.card_plays:,}</td><td class=n>{v.machines}</td>"
            f"<td>{sparkline(v.trend, f'{v.venue_name}, last {TREND_WEEKS} weeks')}</td></tr>" for v in week.venues]
    total_change = None if prev is None else week.total - prev
    rows.append(f"<tr class=total><td>All venues</td><td class=n>{eur(week.total)}</td>{delta(total_change, week.change_pct)}"
                f"<td class=n>{eur(sum(v.coins for v in week.venues))}</td><td class=n>{sum(v.tokens for v in week.venues):,}</td>"
                f"<td class=n>{sum(v.card_plays for v in week.venues):,}</td><td class=n>{sum(v.machines for v in week.venues)}</td>"
                "<td></td></tr>")
    body = (f"<h1>Takings, week of {fmt_week(week.week_start)}</h1>"
            f"<p class=sub>Monday {week.week_start:%d %b} to Sunday {fmt_week(week.week_end)}: the last complete week. "
            f"Data through {fmt_week(week.data_through)}; the trend starts {fmt_week(week.first_week)}."
            f"{in_progress}</p>{tiles}<div class=card><table>{head}{''.join(rows)}</table></div>"
            f"<p class=muted>Takings are the cash counted from the machines (coins, tokens at 0.50 EUR and member-card plays). "
            f"Change is against the week before.</p>"
            f"<h2>Run history</h2>{history_table(runs)}{builder_note(user)}")
    log.info("page shown", week=week.week_start.isoformat(), venues=len(week.venues), runs=len(runs))
    return page("Weekly takings", user, body)


WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/runs/{week_start}", response_class=HTMLResponse)
def run_page(week_start: str, request: Request):
    """One run, as recorded: its numbers come from the CSV the job wrote, so the page matches the file finance gets."""
    user = current_user(request.headers)
    try:
        wk = date.fromisoformat(week_start) if WEEK_RE.match(week_start) else None
    except ValueError:
        wk = None
    run = store.run_for(wk) if wk else None
    if not run:
        log.warn("run not found", week=week_start[:20])
        raise HTTPException(404, "no run for that week")
    path = store.FILES / run["file"]
    if not path.is_file():
        log.error("run's CSV is missing", week=week_start, file=run["file"])
        raise HTTPException(404, "the run's CSV is missing")
    with path.open(newline="") as f:
        lines = list(csv.DictReader(f))
    rows = []
    for r in lines:
        change = float(r["change_eur"]) if r["change_eur"] else None
        pct = float(r["change_pct"]) if r["change_pct"] else None
        name = esc(r["venue_name"]) + ("" if r["venue_code"] == "TOTAL" else f" <span class=city>{esc(r['venue_code'])} · {esc(r['city'])}</span>")
        rows.append(f"<tr{' class=total' if r['venue_code'] == 'TOTAL' else ''}><td>{name}</td><td class=n>{eur(r['amount_eur'])}</td>"
                    f"{delta(change, pct)}<td class=n>{eur(r['coins_eur'])}</td><td class=n>{int(r['tokens']):,}</td>"
                    f"<td class=n>{int(r['card_plays']):,}</td><td class=n>{r['machines']}</td></tr>")
    wk_end = date.fromisoformat(lines[0]["week_end"]) if lines else wk
    body = (f"<p><a href='../'>&larr; Weekly takings</a></p><h1>Run: week of {fmt_week(wk)}</h1>"
            f"<p class=sub>Monday {wk:%d %b} to Sunday {fmt_week(wk_end)}. Last run {run['ran_local']:%Y-%m-%d %H:%M}"
            f"{f' (run {run['runs']} times; each run replaced the one before)' if run['runs'] > 1 else ''}, "
            f"on data through {fmt_week(run['data_through'])}.</p>"
            f"<p><a class=btn href='../download/{esc(run['file'])}'>Download CSV</a> <span class=muted>{esc(run['file'])}</span></p>"
            "<div class=card><table><tr><th>Venue</th><th class=n>Takings EUR</th><th class=n>Change</th><th class=n>%</th>"
            "<th class=n>Coins EUR</th><th class=n>Tokens</th><th class=n>Card plays</th><th class=n>Machines</th></tr>"
            + "".join(rows) + "</table></div>" + builder_note(user))
    log.info("run shown", week=week_start)
    return page(f"Takings, week of {fmt_week(wk)}", user, body)


@app.get("/download/{name}")
def download(name: str):
    path = store.FILES / name
    if not re.fullmatch(r"weekly-takings-\d{4}-\d{2}-\d{2}\.csv", name) or not path.is_file():
        log.warn("CSV not found", file=name[:80])
        raise HTTPException(404, "no such file")
    log.info("CSV downloaded", file=name)
    return FileResponse(path, media_type="text/csv", filename=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
