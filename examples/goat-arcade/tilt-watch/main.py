"""tilt-watch: which pinball cabinets tilt far more than their model does? An Annex web tool (FastAPI).

For the last 30 days in the data, each pinball cabinet's tilt rate (games that ended in a tilt, out of all its games)
next to its model's average on the model's other cabinets. A cabinet over FLAG_FACTOR (3) times that is flagged,
worst first. Reads daily_plays, machine_floor and machine_models through the gateway; no data of its own.
"""
import html
import os
import threading
import time
from datetime import timedelta

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

import tilts
from annex_runtime import (CsrfMiddleware, GatewayError, browser_errors_js, current_user, gateway, log,
                           report_browser_error, request_context)

WINDOW_DAYS = 30
FLAG_FACTOR = float(os.environ.get("FLAG_FACTOR", "3"))
MIN_TILTS = int(os.environ.get("MIN_TILTS", "5"))
CACHE_SECONDS = 300

app = FastAPI(title="tilt-watch")
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
# The data changes at most once a day: one read per view every few minutes is plenty.
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def cached(key: str, load):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def load() -> dict | None:
    """The window's play per pinball cabinet, what stands where now, and the models. None: no play data at all."""
    last = cached("last", lambda: gateway("daily_plays", select="day", order="day.desc", limit=1))
    if not last:
        return None
    last_day = tilts.as_date(last[0]["day"])
    first_day = last_day - timedelta(days=WINDOW_DAYS - 1)
    t0 = time.monotonic()
    plays = cached(f"plays:{first_day}", lambda: gateway(
        "daily_plays", select="day,venue_code,machine_id,title,era,machine_type,games,tilts",
        day=f"gte.{first_day.isoformat()}", machine_type="eq.pinball"))
    floor = cached("floor", lambda: gateway(
        "machine_floor", select="machine_id,model_id,venue_code,venue_name,city,out_of_order,open_ticket"))
    models = cached("models", lambda: gateway("machine_models", select="model_id,title,manufacturer,release_year"))
    log.debug("data loaded", plays=len(plays), floor=len(floor), ms=round((time.monotonic() - t0) * 1000))
    return {"first_day": first_day, "last_day": last_day, "plays": plays, "floor": floor, "models": models}


# --- page -------------------------------------------------------------------------------------------------------
def e(s) -> str:
    return html.escape(str(s))


def venue_cell(c: tilts.Cabinet, venues: dict) -> str:
    if c.venue_code:
        name = venues.get(c.venue_code, c.venue_code)
        return f'<span class="venue">{e(c.venue_code)}</span> <span class="sub">{e(name)}</span>'
    where = f" (last played at {e(c.last_venue)})" if c.last_venue else ""
    return f'<span class="sub">not on a floor now{where}</span>'


def state_tags(c: tilts.Cabinet) -> str:
    if c.out_of_order:
        return '<span class="tag off">out of order</span>'
    if c.open_ticket:
        return '<span class="tag ticket">open ticket</span>'
    return ""


def row(c: tilts.Cabinet, venues: dict, models: dict, cls: str = "") -> str:
    m = models.get(c.model_id, {})
    made = " · ".join(str(x) for x in (m.get("manufacturer"), m.get("release_year"), c.era) if x)
    ratio = tilts.times(c.ratio) if c.ratio is not None else ("only one" if c.peers == 0 else "others: none")
    peers = f"{c.peers} other cabinet{'s' if c.peers != 1 else ''}, {c.peer_games:,} games" if c.peers else "no other cabinet"
    return (f'<tr class="{cls}" id="m{c.machine_id}"><td><b>{e(c.title)}</b> <span class="sub">#{c.machine_id}</span>'
            f'{state_tags(c)}<div class="sub">{e(made)}</div></td>'
            f'<td>{venue_cell(c, venues)}</td>'
            f'<td class="num">{c.games:,}</td><td class="num">{c.tilts:,}</td>'
            f'<td class="num"><b>{tilts.pct(c.rate)}</b></td>'
            f'<td class="num">{tilts.pct(c.model_rate)}<div class="sub">{e(peers)}</div></td>'
            f'<td class="num ratio">{e(ratio)}</td></tr>')


def page(user, data: dict) -> str:
    cabs = tilts.cabinets(data["plays"], data["floor"], data["models"])
    hits = tilts.flagged(cabs, FLAG_FACTOR, MIN_TILTS)
    venues = {f["venue_code"]: f"{f['venue_name']}, {f['city']}" for f in data["floor"]}
    models = {m["model_id"]: m for m in data["models"]}
    hit_ids = {c.machine_id for c in hits}
    total_games = sum(c.games for c in cabs)
    total_tilts = sum(c.tilts for c in cabs)
    first, last = data["first_day"], data["last_day"]
    span = f"{first:%-d %b} to {last:%-d %b %Y}"
    head = ('<tr><th>Cabinet</th><th>Stands at</th><th class="num">Games</th><th class="num">Tilts</th>'
            '<th class="num">Tilt rate</th><th class="num">Model average</th><th class="num">× model</th></tr>')
    hit_rows = "".join(row(c, venues, models, "top" if i == 0 else "hit") for i, c in enumerate(hits)) or (
        f'<tr><td colspan="7" class="empty">No cabinet tilts more than {FLAG_FACTOR:g}× its model\'s average.</td></tr>')
    all_rows = "".join(row(c, venues, models, "hit" if c.machine_id in hit_ids else "") for c in tilts.ranked(cabs))
    worst = hits[0] if hits else None
    headline = (f"<b>{e(worst.title)} #{worst.machine_id}</b> ({e(venues.get(worst.venue_code, worst.last_venue or '?'))}) "
                f"tilts {tilts.pct(worst.rate)} of its games; the other {e(worst.title)} cabinets, "
                f"{tilts.pct(worst.model_rate)}." if worst else "Nothing stands out.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Tilt watch · Goat Arcade</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --ink:#1d1b2b; --mute:#6b6880; --line:#e7e4ef; --bg:#f7f6fa; --card:#fff; --accent:#d9480f; --head:#1d1b2b }}
* {{ box-sizing:border-box }}
body {{ margin:0; font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg) }}
header {{ background:var(--head); color:#fff; padding:14px 24px; display:flex; align-items:center; gap:14px; flex-wrap:wrap }}
header h1 {{ font-size:19px; margin:0 }}
header .meta {{ margin-left:auto; color:#c8c5d8; font-size:13px }}
main {{ max-width:1180px; margin:0 auto; padding:20px 16px 48px }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:16px }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px }}
.tile b {{ font-size:28px; display:block; font-variant-numeric:tabular-nums }}
.tile span {{ color:var(--mute); font-size:13px }}
.tile.alert b {{ color:var(--accent) }}
p.headline {{ background:#fff8e1; border:1px solid #f2d98a; border-radius:10px; padding:10px 16px; margin:0 0 22px }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:22px; overflow:hidden }}
section h2, summary {{ font-size:16px; font-weight:700; margin:0; padding:14px 16px 4px }}
summary {{ cursor:pointer; padding-bottom:12px }}
p.note {{ margin:0; padding:0 16px 10px; color:var(--mute); font-size:13px }}
.scroll {{ overflow-x:auto }}
table {{ width:100%; border-collapse:collapse }}
th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.4px; color:var(--mute); font-weight:600;
  padding:8px 16px; border-top:1px solid var(--line); background:#fbfaff }}
td {{ padding:9px 16px; border-top:1px solid var(--line); vertical-align:top }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
td.empty {{ color:var(--mute); text-align:center; padding:18px }}
td.ratio {{ font-weight:700 }}
tr.hit td.ratio, tr.top td.ratio {{ color:var(--accent) }}
tr.top td {{ background:#fff8e1 }}
.sub {{ color:var(--mute); font-size:12.5px }}
.venue {{ display:inline-block; font-size:11.5px; font-weight:700; padding:1px 6px; border-radius:4px; background:#efeaf9; color:#4b3c8c }}
.tag {{ font-size:11px; font-weight:600; padding:1px 6px; border-radius:4px; margin-left:6px; vertical-align:1px }}
.tag.off {{ background:#fde8e1; color:var(--accent) }}
.tag.ticket {{ background:#efeaf9; color:#4b3c8c }}
footer {{ color:var(--mute); font-size:12.5px; text-align:center }}
</style></head><body>
<header><h1>Tilt watch</h1>
<span class="meta">Goat Arcade pinball · signed in as {e(user.name)} · {e(span)}</span></header>
<main>
<div class="tiles">
  <div class="tile{" alert" if hits else ""}"><b>{len(hits)}</b><span>over {FLAG_FACTOR:g}× their model's average</span></div>
  <div class="tile"><b>{len(cabs)}</b><span>pinball cabinets played</span></div>
  <div class="tile"><b>{tilts.pct(total_tilts / total_games if total_games else None)}</b><span>tilt rate, all pinball</span></div>
  <div class="tile"><b>{total_games:,}</b><span>games in {WINDOW_DAYS} days</span></div>
</div>
<p class="headline">{headline}</p>

<section id="flagged"><h2>Flagged, worst first</h2>
<p class="note">The last {WINDOW_DAYS} days in the data ({e(span)}). Tilt rate: games that ended in a tilt, out of all the
cabinet's games. Model average: the same rate on the model's <em>other</em> cabinets, so a cabinet isn't part of its own
yardstick. Flagged over {FLAG_FACTOR:g}×, with at least {MIN_TILTS} tilts (fewer is chance).
"Stands at" is where it is now; tilts count wherever it stood.</p>
<div class="scroll"><table>{head}{hit_rows}</table></div></section>

<section><details><summary>All {len(cabs)} pinball cabinets</summary>
<p class="note">Highest ratio first. "Only one": the only cabinet of its model, so there's nothing to compare with.</p>
<div class="scroll"><table>{head}{all_rows}</table></div></details></section>

<footer>A high tilt rate usually means a tilt bob set too sensitive, a loose leg or a cabinet that rocks: worth a look.</footer>
</main><script src="/annex-errors.js"></script></body></html>"""


def message_page(text: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Tilt watch · Goat Arcade</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>Tilt watch</h1><p>{e(text)}</p><p><a href="./">Try again</a></p>
<script src="/annex-errors.js"></script></body></html>"""


# Plain `def` endpoints run in a thread pool, so blocking gateway calls are fine.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request.headers)
    try:
        data = load()
    except GatewayError as ex:
        log.error("gateway read failed", status=ex.status, error=str(ex))
        return HTMLResponse(message_page("The play data isn't available right now."), status_code=502)
    if data is None:
        log.warn("no play data")
        return HTMLResponse(message_page("There's no play data yet."), status_code=503)
    return page(user, data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
