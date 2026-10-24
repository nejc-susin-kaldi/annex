"""venue-pulse: how is my venue doing this week against last? An Annex web tool (FastAPI).

The venue follows the user: a choice they made (stored per login in the app's own Postgres), else their default
from HOME_VENUES, else they pick one. Everyone signed in may look at any venue.
"""
import html
import os
import threading
import time
from datetime import date, timedelta

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

import pulse
import store
from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, gateway, log,
                           report_browser_error, request_context)

HOME_VENUES = pulse.parse_home_venues(os.environ.get("HOME_VENUES", ""))
IDLE_GAMES_7D = int(os.environ.get("IDLE_GAMES_7D", "20"))
CACHE_SECONDS = 300

app = FastAPI(title="venue-pulse")
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


# --- data -------------------------------------------------------------------------------------------------------
# The data changes at most once a day, so the gateway reads are cached for a few minutes: one page load is fast.
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def cached(key: str, load):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    t0 = time.monotonic()
    value = load()
    log.debug("gateway read", key=key, ms=round((time.monotonic() - t0) * 1000))
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def load_data() -> dict:
    """Everything the page needs, for all venues (one read each; the page picks its venue in code)."""
    def base():
        first = gateway("daily_plays", select="day", order="day.asc", limit=1)
        last = gateway("daily_plays", select="day", order="day.desc", limit=1)
        venues = gateway("venues", select="venue_code,venue_name,city", order="venue_name")
        return first, last, venues

    first, last, venues = cached("base", base)
    if not last:
        return {"venues": venues, "first_day": None, "last_day": None}
    first_day, last_day = pulse.as_date(first[0]["day"]), pulse.as_date(last[0]["day"])
    since = (last_day - timedelta(days=29)).isoformat()
    plays = cached(f"plays:{since}", lambda: gateway(
        "daily_plays", select="day,venue_code,machine_id,title,games,revenue_eur", day=f"gte.{since}"))
    floor = cached("floor", lambda: gateway(
        "machine_floor", select="machine_id,venue_code,title,era,machine_type,condition,games_7d,games_30d,"
                                "last_played_at,open_ticket,out_of_order", order="title"))
    return {"venues": venues, "first_day": first_day, "last_day": last_day, "plays": plays, "floor": floor}


# --- which venue ------------------------------------------------------------------------------------------------
def resolve_venue(login: str, asked: str | None, codes: set[str]) -> tuple[str | None, str]:
    """(venue_code, why): ?venue= on the URL (a look, not saved), the login's saved choice, their default, or none."""
    if asked and asked.upper() in codes:
        return asked.upper(), "link"
    try:
        saved = store.get_choice(login) if login else None
    except Exception:
        log.exception("could not read the saved venue", login=login)
        saved = None
    if saved in codes:
        return saved, "saved"
    default = HOME_VENUES.get(login)
    if default in codes:
        return default, "default"
    return None, "none"


@app.post("/venue")
def choose_venue(request: Request, venue: str = Form(...)):
    user = current_user(request.headers)
    venue = venue.strip().upper()
    try:
        codes = {v["venue_code"] for v in load_data()["venues"]}
    except GatewayError as e:
        log.error("gateway read failed", step="choose venue", status=e.status)
        return HTMLResponse(page("Venue pulse", error_box("The arcade data can't be read right now. Try again in a minute.")), 502)
    if venue not in codes:
        log.warn("unknown venue chosen", venue=venue)
        return RedirectResponse("/", status_code=303)
    try:
        store.set_choice(user.login, venue)
        log.info("venue chosen", venue=venue)
    except Exception:
        log.exception("could not save the venue choice", venue=venue)
        return RedirectResponse(f"/?venue={venue}", status_code=303)
    return RedirectResponse("/", status_code=303)


# --- pages ------------------------------------------------------------------------------------------------------
def eur(x: float) -> str:
    return f"€{x:,.0f}"


def change_badge(now: float, before: float) -> str:
    pct = pulse.pct_change(now, before)
    if pct is None:
        return '<span class="chg flat">new</span>' if now else '<span class="chg flat">–</span>'
    cls = "up" if pct > 0.5 else "down" if pct < -0.5 else "flat"
    arrow = "▲" if cls == "up" else "▼" if cls == "down" else "■"
    return f'<span class="chg {cls}">{arrow} {pct:+.0f}%</span>'


def fmt_range(a: date, b: date) -> str:
    return f"{a:%a %-d %b} – {b:%a %-d %b %Y}"


def error_box(text: str) -> str:
    return f'<div class="card error">{html.escape(text)}</div>'


def picker(venues: list[dict], current: str | None, compact: bool) -> str:
    opts = "".join(f'<option value="{html.escape(v["venue_code"])}"{" selected" if v["venue_code"] == current else ""}>'
                   f'{html.escape(v["venue_name"])} ({html.escape(v["venue_code"])})</option>' for v in venues)
    if compact:
        return (f'<form method="post" action="/venue" class="picker"><label for="venue">Venue</label>'
                f'<select id="venue" name="venue">{opts}</select><button>Show</button></form>')
    cards = "".join(
        f'<form method="post" action="/venue"><input type="hidden" name="venue" value="{html.escape(v["venue_code"])}">'
        f'<button class="venue-card"><b>{html.escape(v["venue_name"])}</b><span>{html.escape(v["city"])} · '
        f'{html.escape(v["venue_code"])}</span></button></form>' for v in venues)
    return f'<div class="venue-grid">{cards}</div>'


def chart(series: list[tuple[date, int]], this_start: date) -> str:
    """Games per day as an inline SVG bar chart; the last 7 days are highlighted."""
    w, h, pad_l, pad_b, pad_t = 700, 220, 40, 34, 16
    top = max([g for _, g in series] + [1])
    step = 10 ** max(len(str(top)) - 1, 0)
    top = ((top + step - 1) // step) * step
    bw = (w - pad_l) / len(series)
    ih = h - pad_b - pad_t
    parts = []
    for frac in (0, 0.5, 1):
        y = pad_t + ih * (1 - frac)
        parts.append(f'<line x1="{pad_l}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{int(top * frac):,}</text>')
    for i, (d, g) in enumerate(series):
        bh = ih * g / top
        x = pad_l + i * bw + bw * 0.15
        cls = "bar this" if d >= this_start else "bar"
        parts.append(f'<rect x="{x:.1f}" y="{pad_t + ih - bh:.1f}" width="{bw * 0.7:.1f}" height="{bh:.1f}" rx="3" '
                     f'class="{cls}"><title>{d:%a %-d %b}: {g:,} games</title></rect>'
                     f'<text x="{x + bw * 0.35:.1f}" y="{h - 18}" class="ax" text-anchor="middle">{d:%a}</text>'
                     f'<text x="{x + bw * 0.35:.1f}" y="{h - 4}" class="ax dim" text-anchor="middle">{d:%-d.%-m.}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Games per day, last {len(series)} days" '
            f'class="chart">{"".join(parts)}</svg>')


def venue_page(d: dict, venue: dict, why: str, login: str) -> str:
    code, last_day = venue["venue_code"], d["last_day"]
    names = {v["venue_code"]: v for v in d["venues"]}
    s = pulse.week_summary(d["plays"], code, last_day)
    floor_here = [m for m in d["floor"] if m["venue_code"] == code]
    for m in floor_here:  # machines on the floor with no play in either week still get a row
        s["machines"].setdefault(m["machine_id"], pulse.MachineWeek(m["machine_id"], m["title"]))
    on_floor = {m["machine_id"]: m for m in floor_here}
    machines = sorted(s["machines"].values(), key=lambda m: (-m.rev_this, m.title))
    up, down = pulse.movers(machines)
    series = pulse.games_per_day(d["plays"], code, last_day)
    idle = sorted((m for m in floor_here if (m["games_7d"] or 0) < IDLE_GAMES_7D), key=lambda m: (m["games_7d"] or 0))
    rates = pulse.model_rates(d["plays"], d["floor"], last_day)
    rotation = pulse.rotation_candidates(rates, code, {m["title"] for m in floor_here})

    why_text = {"saved": "your saved venue", "default": "your home venue", "link": "from the link (not saved)"}[why]
    kpis = f"""
<section class="kpis">
  <div class="card kpi"><span class="lbl">Games, last 7 days</span><b>{s['games_this']:,}</b>
    <span class="sub">{change_badge(s['games_this'], s['games_last'])} vs {s['games_last']:,} the week before</span></div>
  <div class="card kpi"><span class="lbl">Revenue, last 7 days</span><b>{eur(s['rev_this'])}</b>
    <span class="sub">{change_badge(s['rev_this'], s['rev_last'])} vs {eur(s['rev_last'])} the week before</span></div>
  <div class="card kpi"><span class="lbl">Machines on the floor</span><b>{len(floor_here)}</b>
    <span class="sub">{sum(1 for m in floor_here if m['out_of_order'])} out of order ·
    {sum(1 for m in floor_here if m['open_ticket'])} with an open ticket</span></div>
</section>"""

    def mover_list(ms, empty):
        if not ms:
            return f'<p class="muted">{empty}</p>'
        return "<ul class='movers'>" + "".join(
            f"<li><span>{html.escape(m.title)} <small>#{m.machine_id}</small></span>"
            f"<span class='num'>{eur(m.rev_last)} → {eur(m.rev_this)} <b class='{'up' if m.rev_change > 0 else 'down'}'>"
            f"{'+' if m.rev_change > 0 else '−'}{eur(abs(m.rev_change))}</b></span></li>" for m in ms) + "</ul>"

    movers_html = f"""
<section class="two">
  <div class="card"><h3>Biggest risers</h3>{mover_list(up, "Nothing earned more than the week before.")}</div>
  <div class="card"><h3>Biggest fallers</h3>{mover_list(down, "Nothing earned less than the week before.")}</div>
</section>"""

    def state(m):
        tags = []
        if m.get("out_of_order"):
            tags.append('<span class="tag bad">out of order</span>')
        elif m.get("open_ticket"):
            tags.append('<span class="tag warn">open ticket</span>')
        return "".join(tags) or '<span class="muted">playable</span>'

    if idle:
        idle_rows = "".join(
            f"<tr><td>{html.escape(m['title'])} <small>#{m['machine_id']}</small></td>"
            f"<td>{html.escape(m['era'] or m['machine_type'])}</td><td class='num'>{m['games_7d'] or 0}</td>"
            f"<td class='num'>{m['games_30d'] or 0}</td><td>{state(m)}</td></tr>" for m in idle)
        idle_html = (f"<table><thead><tr><th>Machine</th><th>Era / type</th><th class='num'>Games 7d</th>"
                     f"<th class='num'>Games 30d</th><th>State</th></tr></thead><tbody>{idle_rows}</tbody></table>")
    else:
        idle_html = f'<p class="muted">Every machine had at least {IDLE_GAMES_7D} games in the last 7 days.</p>'

    if rotation:
        def place(v):
            return html.escape(names[v]["city"] if v in names else v)

        def broken_note(title):
            # a model that earns little here because its cabinet is broken wants a technician, not a move
            here = [m for m in floor_here if m["title"] == title]
            if all(m["out_of_order"] for m in here):
                return ' <span class="tag bad">out of order here: fix first</span>'
            if any(m["open_ticket"] for m in here):
                return ' <span class="tag warn">open ticket here</span>'
            return ""

        def cabs(n):
            return f"{n} cabinet{'s' if n != 1 else ''}"
        rot_rows = "".join(
            f"<li><b>{html.escape(c['title'])}</b> earns <b>{eur(c['there']['rate'])}/day</b> in "
            f"{place(c['there_venue'])} <small>({html.escape(c['there_venue'])}, {cabs(c['there']['cabinets'])})</small>"
            f" against <b>{eur(c['here']['rate'])}/day</b> here <small>({cabs(c['here']['cabinets'])})</small>"
            f"{broken_note(c['title'])}</li>" for c in rotation)
        rot_html = f"<ul class='rot'>{rot_rows}</ul>"
    else:
        titles = {m["title"] for m in floor_here}
        best_here = sum(1 for t in titles if (r := rates.get(t, {})).get(code) and
                        r[code]["rate"] >= max(x["rate"] for v, x in r.items() if v == code or x["days"] >= 7))
        rot_html = (f'<p class="muted">No model on this floor earns clearly more somewhere else. {best_here} of the '
                    f'{len(titles)} models here earn the most per cabinet at this venue.</p>')

    mrows = []
    for m in machines:
        fm = on_floor.get(m.machine_id)
        where = "" if fm else ' <span class="tag">moved away</span>'
        mrows.append(
            f"<tr><td>{html.escape(m.title)} <small>#{m.machine_id}</small>{where}</td>"
            f"<td class='num'>{m.games_this:,}</td><td class='num'>{eur(m.rev_last)}</td>"
            f"<td class='num'><b>{eur(m.rev_this)}</b></td><td class='num'>{change_badge(m.rev_this, m.rev_last)}</td>"
            f"<td>{state(fm) if fm else ''}</td></tr>")
    table = (f"<table><thead><tr><th>Machine</th><th class='num'>Games, last 7 days</th><th class='num'>Revenue, week before</th>"
             f"<th class='num'>Revenue, last 7 days</th><th class='num'>Change</th><th>State</th></tr></thead>"
             f"<tbody>{''.join(mrows)}</tbody></table>")

    body = f"""
<header class="top">
  <div><h1><span class="ball"></span>{html.escape(venue['venue_name'])}</h1>
  <p class="muted">{html.escape(venue['city'])} · {html.escape(code)} · {why_text}</p></div>
  {picker(d['venues'], code, compact=True)}
</header>
<p class="range">Last 7 days in the data: <b>{fmt_range(s['this'].start, s['this'].end)}</b>, against
{fmt_range(s['prev'].start, s['prev'].end)}. The data runs from {d['first_day']:%-d %b %Y} to {last_day:%-d %b %Y}.</p>
{kpis}
<section class="card"><h2>Games per day</h2><p class="muted">The last 14 days in the data; the last 7 are highlighted.</p>
{chart(series, s['this'].start)}</section>
{movers_html}
<section class="card"><h2>Idle machines</h2>
<p class="muted">On the floor now with fewer than {IDLE_GAMES_7D} games in the last 7 days.</p>{idle_html}</section>
<section class="card"><h2>Rotation candidates</h2>
<p class="muted">Models on this floor that earn clearly more (at least 1.5×, €5/day more) per cabinet per day at another
venue, over the last 30 days in the data. Revenue is credits times price, from the play counters.</p>{rot_html}</section>
<section class="card"><h2>Every machine</h2>{table}</section>"""
    return page(f"Venue pulse · {venue['venue_name']}", body, login)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, venue: str | None = None):
    user = current_user(request.headers)
    try:
        d = load_data()
    except GatewayError as e:
        log.error("gateway read failed", step="load page", status=e.status)
        return HTMLResponse(page("Venue pulse", error_box(
            "The arcade data can't be read right now. Try again in a minute."), user.login), 502)
    by_code = {v["venue_code"]: v for v in d["venues"]}
    code, why = resolve_venue(user.login, venue, set(by_code))
    if code is None:
        body = (f'<header class="top"><div><h1><span class="ball"></span>Venue pulse</h1>'
                f'<p class="muted">Hi {html.escape(user.name)}. Which venue do you want to look at? '
                f'We\'ll remember it for next time; you can switch any time.</p></div></header>'
                f'{picker(d["venues"], None, compact=False)}')
        return page("Venue pulse", body, user.login)
    if d["last_day"] is None:
        return page("Venue pulse", error_box("There is no play data yet."), user.login)
    log.info("venue shown", venue=code, why=why)
    return venue_page(d, by_code[code], why, user.login)


STYLE = """
:root{--bg:#f6f4f9;--card:#fff;--ink:#1d1730;--muted:#6b6480;--line:#e6e1ee;--accent:#5b2a86;--accent2:#f2a900;
--up:#1b7f4b;--down:#b3261e;--bar:#c9bfe0;--bar-this:#5b2a86}
@media (prefers-color-scheme:dark){:root{--bg:#141020;--card:#1e1830;--ink:#ece8f5;--muted:#a49cba;--line:#2f2745;
--accent:#b48ee0;--accent2:#f2b632;--up:#5fd08f;--down:#ff8a80;--bar:#3c3159;--bar-this:#b48ee0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1060px;margin:0 auto;padding:20px 16px 48px}
.brand{background:var(--accent);color:#fff;padding:10px 16px;font-weight:600;letter-spacing:.02em}
.brand .wrap{max-width:1060px;margin:0 auto;display:flex;justify-content:space-between;gap:12px}
.brand small{opacity:.8;font-weight:400}
h1{margin:0;font-size:1.7rem;display:flex;align-items:center;gap:10px}h2{margin:0 0 4px;font-size:1.1rem}
h3{margin:0 0 8px;font-size:1rem}.ball{width:16px;height:16px;border-radius:50%;flex:none;
background:radial-gradient(circle at 35% 35%,#fff,#bbb 40%,#666);box-shadow:0 0 0 3px var(--accent2)}
.muted{color:var(--muted);margin:2px 0 10px}small{color:var(--muted)}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:8px}
.picker{display:flex;gap:8px;align-items:center}.picker label{color:var(--muted)}
select,button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--ink)}button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}
.range{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent2);border-radius:8px;
padding:8px 12px;margin:12px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.kpis .card{margin:0}
.kpis{margin-bottom:16px}.kpi{display:flex;flex-direction:column;gap:2px}.kpi b{font-size:1.9rem}
.lbl{color:var(--muted);font-size:.85rem;text-transform:uppercase;letter-spacing:.04em}.sub{color:var(--muted);font-size:.9rem}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.two .card{margin-bottom:16px}
.chg{font-weight:600;white-space:nowrap}.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
.chart{width:100%;height:auto;display:block}.chart .bar{fill:var(--bar)}.chart .bar.this{fill:var(--bar-this)}
.chart .grid{stroke:var(--line)}.chart .ax{fill:var(--muted);font-size:11px}.chart .ax.dim{font-size:10px;opacity:.8}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{padding:7px 8px;border-bottom:1px solid var(--line);
text-align:left}th{color:var(--muted);font-weight:500;font-size:.82rem}.num{text-align:right;font-variant-numeric:tabular-nums}
.movers,.rot{list-style:none;margin:0;padding:0}.movers li,.rot li{padding:7px 0;border-bottom:1px solid var(--line)}
.movers li{display:flex;justify-content:space-between;gap:12px}.movers li:last-child,.rot li:last-child{border:0}
.tag{display:inline-block;font-size:.75rem;padding:1px 8px;border-radius:99px;background:var(--line);color:var(--ink)}
.tag.bad{background:var(--down);color:#fff}.tag.warn{background:var(--accent2);color:#1d1730}
.error{border-left:4px solid var(--down)}
.venue-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:16px}
.venue-grid form{margin:0}.venue-card{width:100%;text-align:left;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:4px}
.venue-card:hover{border-color:var(--accent)}.venue-card span{color:var(--muted);font-size:.9rem}
@media (max-width:640px){table{font-size:.85rem}th,td{padding:6px 4px}.kpi b{font-size:1.5rem}}
"""


def page(title: str, body: str, login: str = "") -> str:
    who = f"<small>signed in as {html.escape(login)}</small>" if login else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{STYLE}</style></head>
<body><div class="brand"><div class="wrap"><span>Goat Arcade · Venue pulse</span>{who}</div></div>
<main>{body}</main><script src="/annex-errors.js"></script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port, home_venues=len(HOME_VENUES))
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
