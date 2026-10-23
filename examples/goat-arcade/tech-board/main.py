"""tech-board: the technicians' morning list, with "I'm on it" claims (FastAPI)."""
import html
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

import board
import store
from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, log,
                           report_browser_error, request_context)

TZ = ZoneInfo("Europe/Ljubljana")
CACHE_S = 60

app = FastAPI(title="tech-board")
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


# The gateway data changes once a day at most: keep it a minute so the page stays fast. Claims are never cached.
_cache: dict = {"at": 0.0, "list": None}
_cache_lock = threading.Lock()


def morning_list() -> board.MorningList:
    with _cache_lock:
        if _cache["list"] is None or time.monotonic() - _cache["at"] > CACHE_S:
            t0 = time.monotonic()
            ml = board.load()
            _cache.update(at=time.monotonic(), list=ml)
            log.info("morning list loaded", out_of_order=len(ml.out_of_order), tickets=len(ml.tickets),
                     rising=len(ml.rising), data_end=ml.data_end.isoformat(), ms=round((time.monotonic() - t0) * 1000))
        return _cache["list"]


e = html.escape


def first_name(name: str, login: str) -> str:
    return (name or login).split()[0]


def claim_cell(kind: str, ref_id: int, claims: dict, me: str) -> str:
    """The "I'm on it" button, or who's on it with a release button."""
    c = claims.get((kind, ref_id))
    hidden = f'<input type="hidden" name="kind" value="{kind}"><input type="hidden" name="ref_id" value="{ref_id}">'
    if c is None:
        return (f'<form method="post" action="claim">{hidden}'
                f'<button class="claim">I\'m on it</button></form>')
    since = c["claimed_at"].astimezone(TZ)
    when = since.strftime("%H:%M") if since.date() == datetime.now(TZ).date() else since.strftime("%-d %b %H:%M")
    who = "You" if c["login"] == me else e(first_name(c["name"], c["login"]))
    mine = " mine" if c["login"] == me else ""
    return (f'<form method="post" action="release" class="claimed{mine}">{hidden}'
            f'<span class="who" title="{e(c["name"])} ({e(c["login"])})">{who} since {when}</span>'
            f'<button class="release" title="Release the claim">Release</button></form>')


def spark(weekly: list[int]) -> str:
    top = max(weekly) or 1
    bars = "".join(f'<i style="height:{max(2, round(18 * w / top))}px" title="{w}"></i>' for w in weekly)
    return f'<span class="spark" aria-label="faults per week: {", ".join(map(str, weekly))}">{bars}</span>'


def page(user, ml: board.MorningList, claims: dict) -> str:
    me = user.login
    venue = lambda code: e(ml.venues.get(code, code or "in storage"))  # noqa: E731

    out_rows = "".join(
        f'<tr id="machine-{m["machine_id"]}"><td><b>{e(m["title"])}</b><div class="sub">#{m["machine_id"]} · '
        f'{e(m["serial_no"])}{" · " + e(m["era"]) if m["era"] else ""}</div></td>'
        f'<td><span class="venue">{e(m["venue_code"])}</span> {venue(m["venue_code"])}</td>'
        f'<td class="num">{board.age_days(m["off_since"], ml.data_end) if m["off_since"] else "?"} d</td>'
        f'<td>{claim_cell("machine", m["machine_id"], claims, me)}</td></tr>'
        for m in ml.out_of_order) or '<tr><td colspan="4" class="empty">Nothing is out of order. Enjoy it.</td></tr>'

    status = {"open": "open", "in_progress": "in progress", "waiting_parts": "waiting for parts"}
    ticket_rows = "".join(
        f'<tr id="ticket-{t["ticket_id"]}"><td class="num age{" old" if t["age_days"] >= 7 else ""}">'
        f'{t["age_days"]} d</td>'
        f'<td><b>{e(t["title"])}</b>{" <span class=\"tag off\">off the floor</span>" if t["out_of_order"] else ""}'
        f'<div class="sub">#{t["machine_id"]} · ticket {t["ticket_id"]} · {e(status.get(t["status"], t["status"]))}'
        f' · reported by {e(t["reported_by"])}</div></td>'
        f'<td class="symptom">{e(t["symptom"] or "")}</td>'
        f'<td><span class="venue">{e(t["venue_code"])}</span></td>'
        f'<td>{claim_cell("ticket", t["ticket_id"], claims, me)}</td></tr>'
        for t in ml.tickets) or '<tr><td colspan="5" class="empty">No open tickets.</td></tr>'

    def went_off(r: board.Rising) -> str:
        if r.until == ml.data_end:
            return ""
        return f' <span class="tag off" title="Counted to the day it went off the floor">off since {r.until:%-d %b}</span>'

    rising_rows = "".join(
        f'<tr id="rising-{r.machine_id}"{" class=top" if i == 0 else ""}><td><b>{e(r.title)}</b>{went_off(r)}'
        f'<div class="sub">#{r.machine_id} · {r.coil} coil, {r.switch} switch faults in its last week</div></td>'
        f'<td><span class="venue">{e(r.venue_code or "-")}</span> {venue(r.venue_code)}</td>'
        f'<td class="num"><b>{r.recent}</b></td><td class="num">{r.baseline_week:.1f}</td>'
        f'<td class="num">{"x" + format(r.ratio, ".1f") if r.baseline_week >= 1 else "new"}</td>'
        f'<td>{spark(r.weekly)}</td>'
        f'<td>{claim_cell("machine", r.machine_id, claims, me)}</td></tr>'
        for i, r in enumerate(ml.rising)) or '<tr><td colspan="7" class="empty">No machine is faulting more than usual.</td></tr>'

    data_end = ml.data_end.strftime("%a %-d %b %Y")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Tech board · Goat Arcade</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --ink:#1d1b2b; --mute:#6b6880; --line:#e7e4ef; --bg:#f7f6fa; --card:#fff; --accent:#d9480f; --ok:#2b8a3e;
  --gold:#f2b705; --head:#1d1b2b; }}
* {{ box-sizing:border-box }}
body {{ margin:0; font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg) }}
header {{ background:var(--head); color:#fff; padding:14px 24px; display:flex; align-items:center; gap:14px; flex-wrap:wrap }}
header h1 {{ font-size:19px; margin:0; letter-spacing:.2px }}
header .ball {{ width:22px; height:22px; border-radius:50%; background:radial-gradient(circle at 35% 30%,#fff,#c9c9d6 45%,#6d6d80) }}
header .meta {{ margin-left:auto; color:#c8c5d8; font-size:13px }}
main {{ max-width:1180px; margin:0 auto; padding:20px 16px 48px }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:22px }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px }}
.tile b {{ font-size:28px; display:block; font-variant-numeric:tabular-nums }}
.tile span {{ color:var(--mute); font-size:13px }}
.tile.alert b {{ color:var(--accent) }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:22px; overflow:hidden }}
section h2 {{ font-size:16px; margin:0; padding:14px 16px 4px }}
section p.note {{ margin:0; padding:0 16px 10px; color:var(--mute); font-size:13px }}
.scroll {{ overflow-x:auto }}
table {{ width:100%; border-collapse:collapse }}
th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.4px; color:var(--mute); font-weight:600;
  padding:8px 16px; border-top:1px solid var(--line); background:#fbfaff }}
td {{ padding:9px 16px; border-top:1px solid var(--line); vertical-align:top }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
td.empty {{ color:var(--mute); text-align:center; padding:18px }}
.sub {{ color:var(--mute); font-size:12.5px }}
.symptom {{ max-width:340px }}
.venue {{ display:inline-block; font-size:11.5px; font-weight:700; padding:1px 6px; border-radius:4px; background:#efeaf9; color:#4b3c8c }}
.tag {{ font-size:11px; font-weight:600; padding:1px 6px; border-radius:4px; margin-left:6px; vertical-align:1px }}
.tag.off {{ background:#fde8e1; color:var(--accent) }}
.age.old {{ color:var(--accent); font-weight:700 }}
tr.top td {{ background:#fff8e1 }}
tr:target td {{ background:#fff3bf }}
.spark {{ display:inline-flex; align-items:flex-end; gap:2px; height:18px }}
.spark i {{ display:block; width:7px; background:#b6acd6; border-radius:1px }}
.spark i:last-child {{ background:var(--accent) }}
form {{ margin:0; display:flex; align-items:center; gap:8px; white-space:nowrap }}
button {{ font:inherit; font-size:13px; border-radius:6px; cursor:pointer; padding:4px 10px }}
button.claim {{ background:var(--ink); color:#fff; border:1px solid var(--ink) }}
button.claim:hover {{ background:var(--accent); border-color:var(--accent) }}
button.release {{ background:none; border:1px solid var(--line); color:var(--mute) }}
.who {{ font-size:13px; font-weight:600; color:var(--ok) }}
.claimed:not(.mine) .who {{ color:#4b3c8c }}
footer {{ color:var(--mute); font-size:12.5px; text-align:center }}
</style></head><body>
<header><span class="ball" aria-hidden="true"></span><h1>Tech board</h1>
<span class="meta">Goat Arcade technicians · signed in as {e(user.name)} · data to {data_end}</span></header>
<main>
<div class="tiles">
  <div class="tile{" alert" if ml.out_of_order else ""}"><b>{len(ml.out_of_order)}</b><span>out of order now</span></div>
  <div class="tile"><b>{len(ml.tickets)}</b><span>open tickets</span></div>
  <div class="tile"><b>{ml.tickets[0]["age_days"] if ml.tickets else 0} d</b><span>oldest open ticket</span></div>
  <div class="tile{" alert" if ml.rising else ""}"><b>{len(ml.rising)}</b><span>rising coil/switch faults</span></div>
</div>

<section id="out"><h2>Out of order now</h2>
<p class="note">Off the floor until a technician fixes them. "Off for" counts from the ticket that took the machine off.</p>
<div class="scroll"><table><tr><th>Machine</th><th>Venue</th><th class="num">Off for</th><th></th></tr>{out_rows}</table></div></section>

<section id="tickets"><h2>Open tickets, oldest first</h2>
<p class="note">Age to the end of the data ({data_end}). A week or older is in orange.</p>
<div class="scroll"><table><tr><th class="num">Age</th><th>Machine</th><th>Symptom</th><th>Venue</th><th></th></tr>{ticket_rows}</table></div></section>

<section id="rising"><h2>Rising faults</h2>
<p class="note">Coil and switch faults in the last 7 days of the data (to {data_end}) against the machine's weekly rate
over the 21 days before. A machine that's off the floor reports nothing, so for it the week is the one before it went off.
Listed at 4 or more faults that week and at least twice the usual rate, worst first. The bars are the four weeks,
the last one in orange.</p>
<div class="scroll"><table><tr><th>Machine</th><th>Venue</th><th class="num">Last week</th><th class="num">Usual / wk</th>
<th class="num">Rise</th><th>4 weeks</th><th></th></tr>{rising_rows}</table></div></section>

<footer>Claims are shared with everyone on the board. Anyone can release one.</footer>
</main><script src="/annex-errors.js"></script></body></html>"""


def error_page(message: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Tech board · Goat Arcade</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>Tech board</h1><p>{e(message)}</p><p><a href="./">Try again</a></p>
<script src="/annex-errors.js"></script></body></html>"""


# Plain `def` endpoints run in a thread pool, so blocking gateway and database calls are fine.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    try:
        ml = morning_list()
    except GatewayError as ex:
        log.error("gateway read failed", status=ex.status, error=str(ex))
        return HTMLResponse(error_page("The machine data isn't available right now."), status_code=502)
    except LookupError as ex:
        log.warn("no machine data", error=str(ex))
        return HTMLResponse(error_page("There's no machine data yet."), status_code=503)
    with store.connect() as conn:
        claims = store.open_claims(conn)
    return page(user, ml, claims)


def _target(kind: str, ref_id: int) -> str:
    return f"./#{kind}-{ref_id}"


@app.post("/claim")
def claim(request: Request, kind: str = Form(...), ref_id: int = Form(...)):
    user = current_user(request.headers)
    if kind not in store.KINDS:
        return PlainTextResponse("kind must be machine or ticket", status_code=400)
    with store.connect() as conn:
        c = store.claim(conn, kind, ref_id, user.login, user.name)
    if c and c["login"] == user.login:
        log.info("claimed", kind=kind, ref_id=ref_id)
    else:
        log.info("claim refused: already claimed", kind=kind, ref_id=ref_id, by=c["login"] if c else None)
    return RedirectResponse(_target(kind, ref_id), status_code=303)


@app.post("/release")
def release(request: Request, kind: str = Form(...), ref_id: int = Form(...)):
    user = current_user(request.headers)
    if kind not in store.KINDS:
        return PlainTextResponse("kind must be machine or ticket", status_code=400)
    with store.connect() as conn:
        c = store.release(conn, kind, ref_id, user.login)
    log.info("released", kind=kind, ref_id=ref_id, claimed_by=c["login"] if c else None)
    return RedirectResponse(_target(kind, ref_id), status_code=303)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
