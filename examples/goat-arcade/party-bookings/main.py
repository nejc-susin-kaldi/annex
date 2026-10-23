"""party-bookings: birthday-party requests from Goat Arcade's website, and the staff page that answers them.

The website form posts each request to Annex's public webhook URL ($ANNEX_HOOKS_URL/website); Annex checks the shared
secret and forwards it to POST /hooks/website here. Staff see the requests behind the login, confirm (venue and slot),
decline with a reason or mark them as contacted. The customer is never emailed by the app: the venue's manager gets
an email for each new request and a confirmation summary to forward (see README).
"""
import html
import json
import os
import threading
import time
from datetime import date, datetime
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import booking
import store
from annex_runtime import (CsrfMiddleware, GatewayError, HookError, NotifyError, browser_errors_js, current_user,
                           gateway, hook_event, log, report_browser_error, request_context, send_email)

TZ = ZoneInfo("Europe/Ljubljana")
MANAGERS = booking.parse_managers(os.environ.get("VENUE_MANAGERS", ""))
PARTY_HOURS = float(os.environ.get("PARTY_HOURS", "2"))
BASE_URL = os.environ.get("ANNEX_BASE_URL", "").rstrip("/")
VENUES_CACHE_S = 600

app = FastAPI(title="party-bookings")
# Refuses state-changing requests from other sites (the contract's "Cross-site requests"); keep it on. Annex's webhook
# delivery sends neither Origin nor Sec-Fetch-Site, so it passes. Added before request_log so a refusal still gets a
# request id and an access line.
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


def today() -> date:
    return datetime.now(TZ).date()


# --- venues (gateway, cached) ---------------------------------------------------------------------------------------
_venues: tuple[float, dict] | None = None
_venues_lock = threading.Lock()


def venues() -> dict[str, dict] | None:
    """{venue_code: {venue_name, city, opening_hours}} of the open venues, or None if the gateway can't be read."""
    global _venues
    with _venues_lock:
        if _venues and time.monotonic() - _venues[0] < VENUES_CACHE_S:
            return _venues[1]
    try:
        rows = gateway("venues", select="venue_code,venue_name,city,opening_hours", order="venue_code")
    except GatewayError as e:
        log.warn("gateway read failed", view="venues", status=e.status)
        return _venues[1] if _venues else None
    found = {r["venue_code"]: r for r in rows}
    with _venues_lock:
        _venues = (time.monotonic(), found)
    return found


def venue_label(code: str, vs: dict | None) -> str:
    v = (vs or {}).get(code)
    return f"{v['venue_name']} ({code})" if v else code


# --- the webhook ------------------------------------------------------------------------------------------------------
@app.post("/hooks/website")
async def website_hook(request: Request, background: BackgroundTasks):
    """One party request from the website form, forwarded by Annex after it checked X-Annex-Hook-Secret."""
    try:
        event = hook_event(request.headers)
    except HookError as e:
        log.warn("webhook refused", reason="not from Annex")
        return JSONResponse({"error": str(e)}, status_code=e.status)
    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    vs = await run_in_threadpool(venues)
    req, problems = booking.parse_request(body, today(), set(vs) if vs else None)
    if body is None:
        problems = ["the body is not JSON"]
    booking_id, status = await run_in_threadpool(store.add_request, event.event_id, req, problems,
                                                 booking.raw_fields(body))
    if booking_id is None:
        log.info("duplicate event ignored", event_id=event.event_id, attempt=event.attempt)
        return {"ok": True, "duplicate": True}
    if req:
        log.info("request received", booking=booking_id, event_id=event.event_id, venue=req.preferred_venue)
        background.add_task(email_new_request, booking_id)
    else:
        # the reasons name fields, never their values
        log.warn("request rejected", booking=booking_id, event_id=event.event_id, reasons="; ".join(problems))
    return {"ok": True, "id": booking_id, "status": status}


# --- email (to staff only: Annex sends to logins and company addresses, never to the customer) ------------------------
def _send(booking_id: int, to: list[str], subject: str, text: str, what: str) -> None:
    try:
        send_email(to, subject, text=text)
    except NotifyError as e:
        log.error("email failed", booking=booking_id, what=what, status=e.status, error=str(e))
        store.note(booking_id, "party-bookings", "email failed", f"{what} to {', '.join(to)}: {e}")
        return
    log.info("email sent", booking=booking_id, what=what, recipients=len(to))
    store.note(booking_id, "party-bookings", "emailed", f"{what} to {', '.join(to)}")


def _contact(b: dict) -> str:
    return " · ".join(x for x in (b["email"], b["phone"]) if x)


def email_new_request(booking_id: int) -> None:
    b = store.get(booking_id)
    manager = booking.manager_for(MANAGERS, b["preferred_venue"])
    if not manager:
        log.warn("no manager for venue", booking=booking_id, venue=b["preferred_venue"])
        return
    vs = venues()
    day = booking.fmt_day(b["req_date"])
    hours = (vs or {}).get(b["preferred_venue"], {}).get("opening_hours")
    problem = booking.slot_problem(hours, b["req_date"], b["req_time"], PARTY_HOURS)
    text = (f"A new birthday-party request came in from the website.\n\n"
            f"Name: {b['name']}\n"
            f"Contact: {_contact(b)}\n"
            f"Venue: {venue_label(b['preferred_venue'], vs)}\n"
            f"When: {day} at {b['req_time']}\n"
            f"Kids: {b['kids']} (turning {b['age']})\n"
            + (f"Note: {b['note']}\n" if b["note"] else "")
            + (f"\nHeads-up: {problem}.\n" if problem else "")
            + f"\nConfirm, decline or mark it as contacted: {BASE_URL}/r/{booking_id}\n"
              f"Reply to the customer yourself; this app never emails them.\n")
    _send(booking_id, [manager], f"Party request: {b['name']}, {day}, {b['preferred_venue']}", text,
          "new request")


def email_confirmed(booking_id: int, by: str) -> None:
    b = store.get(booking_id)
    vs = venues()
    v = (vs or {}).get(b["venue"], {})
    to = [x for x in dict.fromkeys([booking.manager_for(MANAGERS, b["venue"]), by]) if x]
    day = booking.fmt_day(b["slot_date"])
    first = b["name"].split()[0] if b["name"] else "there"
    text = (f"{by} confirmed this birthday party. Forward the part below to the customer"
            f" ({b['email'] or b['phone']}).\n\n"
            f"---\n\n"
            f"Hi {first},\n\n"
            f"Your birthday party at Goat Arcade is booked:\n\n"
            f"  Where: {v.get('venue_name', b['venue'])}, {v.get('city', '')}\n"
            f"  When:  {day}, from {b['slot_time']} ({PARTY_HOURS:g} hours)\n"
            f"  Kids:  {b['kids']} (the birthday child turns {b['age']})\n\n"
            f"We'll have a table ready for you. If anything changes, just reply to this email.\n\n"
            f"See you soon,\nGoat Arcade\n\n"
            f"---\n\nThe request: {BASE_URL}/r/{booking_id}\n")
    _send(booking_id, to, f"Confirmed: party for {b['name']}, {day} {b['slot_time']}, {b['venue']}", text,
          "confirmation")


# --- pages ------------------------------------------------------------------------------------------------------------
def h(x) -> str:
    return html.escape("" if x is None else str(x))


STATUS_LABEL = {"new": "New", "contacted": "Contacted", "confirmed": "Confirmed", "declined": "Declined",
                "rejected": "Rejected"}


def tag(status: str) -> str:
    return f'<span class="tag s-{h(status)}">{h(STATUS_LABEL.get(status, status))}</span>'


@app.get("/", response_class=HTMLResponse)
def index(request: Request, venue: str = "", status: str = ""):
    user = current_user(request.headers)
    venue = venue.upper() if booking.VENUE_RE.match(venue.upper()) else ""
    status = status if status in booking.STATUSES or status == "open" else ""
    vs = venues()
    rows = store.list_bookings(venue, status)
    counts = store.counts()

    venue_opts = '<option value="">All venues</option>' + "".join(
        f'<option value="{h(c)}"{" selected" if c == venue else ""}>{h(v["venue_name"])} ({h(c)})</option>'
        for c, v in (vs or {}).items())
    if venue and venue not in (vs or {}):
        venue_opts += f'<option value="{h(venue)}" selected>{h(venue)}</option>'
    status_opts = "".join(f'<option value="{k}"{" selected" if k == status else ""}>{label}</option>' for k, label in
                          [("", "Any status"), ("open", "Open (new or contacted)")]
                          + [(s, STATUS_LABEL[s]) for s in booking.STATUSES])
    chips = "".join(
        f'<a class="chip" href="/?{urlencode({"status": s, **({"venue": venue} if venue else {})})}">'
        f'{tag(s)} <b>{counts.get(s, 0)}</b></a>' for s in booking.STATUSES)

    trs = []
    for b in rows:
        if b["status"] == "rejected":
            when, slot = '<span class="muted">—</span>', f'<span class="muted">{h(b["reject_reason"])}</span>'
        else:
            when = f'{h(booking.fmt_day(b["req_date"]))} <small>{h(b["req_time"])}</small>'
            slot = (f'{h(b["venue"])} · {h(booking.fmt_day(b["slot_date"]))} {h(b["slot_time"])}'
                    if b["status"] == "confirmed" else
                    f'<span class="muted">{h(b["decline_reason"])}</span>' if b["status"] == "declined" else "")
        kids = f'{b["kids"]} <small>turning {b["age"]}</small>' if b["kids"] else ""
        trs.append(f'<tr class="row-{h(b["status"])}"><td>{tag(b["status"])}</td>'
                   f'<td><a href="/r/{b["id"]}"><b>{h(b["name"]) or "(no name)"}</b></a>'
                   f'<br><small>#{b["id"]} · received {h(booking.fmt_when(b["received_at"].astimezone(TZ)))}</small></td>'
                   f'<td>{h(venue_label(b["preferred_venue"], vs)) if b["preferred_venue"] else ""}</td>'
                   f'<td>{when}</td><td class="num">{kids}</td><td>{slot}</td></tr>')
    table = (f'<table><thead><tr><th>Status</th><th>Who</th><th>Preferred venue</th><th>Preferred date</th>'
             f'<th class="num">Kids</th><th>Booked slot / reason</th></tr></thead><tbody>{"".join(trs)}</tbody></table>'
             if trs else '<p class="muted">No requests match this filter.</p>')
    gw = "" if vs else '<p class="card error">Venue names are unavailable right now (gateway); showing codes.</p>'
    body = f"""
<div class="top"><div><h1><span class="ball"></span>Party bookings</h1>
<p class="muted">Birthday-party requests from the website, newest first, new ones on top.</p></div>
<form class="filters" method="get" action="/"><select name="venue" aria-label="Venue">{venue_opts}</select>
<select name="status" aria-label="Status">{status_opts}</select><button>Filter</button>
{'<a href="/">Clear</a>' if venue or status else ''}</form></div>
<div class="chips">{chips}</div>{gw}
<div class="card">{table}</div>"""
    return page("Party bookings", body, user.login)


def _slot_form(b: dict, vs: dict | None, venue: str, day: str, slot_time: str, warn: str = "") -> str:
    opts = "".join(f'<option value="{h(c)}"{" selected" if c == venue else ""}>{h(v["venue_name"])} ({h(c)})</option>'
                   for c, v in (vs or {}).items())
    if not vs:
        opts = f'<option value="{h(venue)}" selected>{h(venue)}</option>'
    anyway = ""
    if warn:
        anyway = (f'<p class="warnbox">{h(warn)}. Confirm anyway, or change the slot.</p>'
                  '<input type="hidden" name="anyway" value="1">')
    return f"""<form method="post" action="/r/{b['id']}/confirm" class="stack">
<label>Venue <select name="venue">{opts}</select></label>
<div class="row2"><label>Date <input type="date" name="date" value="{h(day)}" required></label>
<label>From <input type="time" name="time" value="{h(slot_time)}" required></label></div>
{anyway}<button>{'Confirm anyway' if warn else 'Save the new slot' if b['status'] == 'confirmed' else 'Confirm booking'}</button>
<small>Emails {h(booking.manager_for(MANAGERS, venue) or 'the manager')} a summary to forward to the customer.</small></form>"""


def detail_page(user, b: dict, *, notice: str = "", error: str = "", form: dict | None = None,
                warn: str = "") -> str:
    vs = venues()
    hist = store.history(b["id"])
    rows = []
    if b["status"] == "rejected":
        raw = b["raw"] or {}
        rows.append(("Why rejected", h(b["reject_reason"])))
        rows += [(k, h(v)) for k, v in raw.items()]
        facts = "".join(f"<tr><th>{h(k)}</th><td>{v}</td></tr>" for k, v in rows)
        main = f'<div class="card"><h2>Rejected request</h2><p class="muted">The website sent something we could ' \
               f'not use. Nothing to do here unless the fields below show a real customer.</p><table class="facts">' \
               f'{facts}</table></div>'
        actions = ""
    else:
        pv = (vs or {}).get(b["preferred_venue"], {})
        req_warn = booking.slot_problem(pv.get("opening_hours"), b["req_date"], b["req_time"], PARTY_HOURS)
        mail = ""
        if b["email"]:
            q = urlencode({"subject": "Your birthday party at Goat Arcade"}, quote_via=quote)
            mail = f'<a href="mailto:{h(b["email"])}?{h(q)}">{h(b["email"])}</a>'
        tel = f'<a href="tel:{h(b["phone"].replace(" ", ""))}">{h(b["phone"])}</a>' if b["phone"] else ""
        rows = [("Email", mail or '<span class="muted">none given</span>'),
                ("Phone", tel or '<span class="muted">none given</span>'),
                ("Preferred venue", h(venue_label(b["preferred_venue"], vs))),
                ("Preferred date", f'{h(booking.fmt_day(b["req_date"]))} at {h(b["req_time"])}'
                                   f' <small>(open {h(booking.hours_text(pv.get("opening_hours"), b["req_date"]))})</small>'
                                   if pv else f'{h(booking.fmt_day(b["req_date"]))} at {h(b["req_time"])}'),
                ("Kids", f'{b["kids"]}, the birthday child turns {b["age"]}'),
                ("Note", h(b["note"]) or '<span class="muted">—</span>')]
        if b["status"] == "confirmed":
            rows.insert(0, ("Booked", f'<b>{h(venue_label(b["venue"], vs))}, {h(booking.fmt_day(b["slot_date"]))} '
                                      f'from {h(b["slot_time"])}</b>'))
        if b["status"] == "declined":
            rows.insert(0, ("Declined", h(b["decline_reason"])))
        facts = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
        hint = f'<p class="warnbox">Heads-up: {h(req_warn)}.</p>' if req_warn else ""
        main = (f'<div class="card"><h2>The request</h2><table class="facts">{facts}</table>{hint}'
                f'<p class="muted small">Reply to the customer yourself (the email link opens your mail program). '
                f'The app never emails customers.</p></div>')
        f = form or {}
        confirm = _slot_form(b, vs, f.get("venue") or b["venue"] or b["preferred_venue"],
                             f.get("date") or str(b["slot_date"] or b["req_date"]),
                             f.get("time") or b["slot_time"] or b["req_time"], warn)
        contacted = (f'<form method="post" action="/r/{b["id"]}/contacted"><button class="ghost">Mark as contacted'
                     f'</button></form>' if b["status"] == "new" else "")
        decline = "" if b["status"] == "declined" else f"""<form method="post" action="/r/{b['id']}/decline"
class="stack"><label>Reason <input name="reason" maxlength="500" required
placeholder="e.g. fully booked that Saturday"></label><button class="danger">Decline</button></form>"""
        actions = f"""<div class="card"><h2>{'Change the booking' if b['status'] == 'confirmed' else 'Confirm'}</h2>
{confirm}</div><div class="card"><h2>Other actions</h2>{contacted}{decline}</div>"""

    items = "".join(f'<li><span class="when">{h(booking.fmt_when(e["at"].astimezone(TZ)))}</span> '
                    f'<b>{h(e["login"])}</b> {h(e["action"])}{(": " + h(e["detail"])) if e["detail"] else ""}</li>'
                    for e in hist)
    msgs = (f'<p class="card ok">{h(notice)}</p>' if notice else "") + \
           (f'<p class="card error">{h(error)}</p>' if error else "")
    received = booking.fmt_when(b["received_at"].astimezone(TZ))
    return page(f"#{b['id']} {b['name']}", f"""
<p><a href="/">&larr; All requests</a></p>
<div class="top"><div><h1><span class="ball"></span>{h(b['name']) or '(no name)'} {tag(b['status'])}</h1>
<p class="muted">Request #{b['id']}, received {h(received)} from the website form.</p></div></div>{msgs}
<div class="two"><div>{main}<div class="card"><h2>History</h2><ul class="hist">{items}</ul></div></div>
<div>{actions}</div></div>""", user.login)


def _load(booking_id: int):
    b = store.get(booking_id)
    if not b:
        return None, HTMLResponse(page("Not found", '<p><a href="/">&larr; All requests</a></p>'
                                       '<div class="card"><h2>No such request</h2></div>'), status_code=404)
    return b, None


DONE = {"confirmed": "Booking confirmed. The summary to forward is on its way to your inbox.",
        "declined": "Request declined.", "contacted": "Marked as contacted."}


@app.get("/r/{booking_id}", response_class=HTMLResponse)
def detail(request: Request, booking_id: int, done: str = ""):
    b, missing = _load(booking_id)
    return missing or detail_page(current_user(request.headers), b, notice=DONE.get(done, ""))


@app.post("/r/{booking_id}/contacted")
def contacted(request: Request, booking_id: int):
    user = current_user(request.headers)
    b, missing = _load(booking_id)
    if missing:
        return missing
    if not store.mark_contacted(booking_id, user.login):
        return HTMLResponse(detail_page(user, b, error="Only a new request can be marked as contacted."), 409)
    log.info("booking contacted", booking=booking_id)
    return RedirectResponse(f"/r/{booking_id}?done=contacted", status_code=303)


@app.post("/r/{booking_id}/decline")
def decline(request: Request, booking_id: int, reason: str = Form("")):
    user = current_user(request.headers)
    b, missing = _load(booking_id)
    if missing:
        return missing
    reason = " ".join(reason.split())[:500]
    if not reason:
        return HTMLResponse(detail_page(user, b, error="Say why it's declined (the customer will ask)."), 400)
    if not store.decline(booking_id, user.login, reason):
        return HTMLResponse(detail_page(user, b, error=f"A {b['status']} request can't be declined."), 409)
    log.info("booking declined", booking=booking_id)
    return RedirectResponse(f"/r/{booking_id}?done=declined", status_code=303)


@app.post("/r/{booking_id}/confirm")
def confirm(request: Request, background: BackgroundTasks, booking_id: int, venue: str = Form(""),
            date_: str = Form("", alias="date"), time_: str = Form("", alias="time"), anyway: str = Form("")):
    user = current_user(request.headers)
    b, missing = _load(booking_id)
    if missing:
        return missing
    form = {"venue": venue, "date": date_, "time": time_}
    vs = venues()
    venue = venue.strip().upper()
    if not (venue in vs if vs else booking.VENUE_RE.match(venue)):
        return HTMLResponse(detail_page(user, b, error="Choose one of the venues.", form=form), 400)
    try:
        day = date.fromisoformat(date_.strip())
    except ValueError:
        return HTMLResponse(detail_page(user, b, error="Give the date of the party.", form=form), 400)
    m = booking.TIME_RE.match(time_.strip())
    if not m:
        return HTMLResponse(detail_page(user, b, error="Give the start time as HH:MM.", form=form), 400)
    slot_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    if day < today():
        return HTMLResponse(detail_page(user, b, error="That date is in the past.", form=form), 400)
    problem = booking.slot_problem((vs or {}).get(venue, {}).get("opening_hours"), day, slot_time, PARTY_HOURS)
    if problem and not anyway:
        return HTMLResponse(detail_page(user, b, form=form, warn=problem[0].upper() + problem[1:]))
    detail = f"{venue}, {booking.fmt_day(day)} from {slot_time}" + (f" (despite: {problem})" if problem else "")
    if not store.confirm(booking_id, user.login, venue, day, slot_time, detail):
        return HTMLResponse(detail_page(user, b, error=f"A {b['status']} request can't be confirmed."), 409)
    log.info("booking confirmed", booking=booking_id, venue=venue, outside_hours=bool(problem))
    background.add_task(email_confirmed, booking_id, user.login)
    return RedirectResponse(f"/r/{booking_id}?done=confirmed", status_code=303)


STYLE = """
:root{--bg:#f6f4f9;--card:#fff;--ink:#1d1730;--muted:#6b6480;--line:#e6e1ee;--accent:#5b2a86;--accent2:#f2a900;
--ok:#1b7f4b;--bad:#b3261e;--new:#f2a900;--contacted:#3a6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#141020;--card:#1e1830;--ink:#ece8f5;--muted:#a49cba;--line:#2f2745;
--accent:#b48ee0;--accent2:#f2b632;--ok:#5fd08f;--bad:#ff8a80;--new:#f2b632;--contacted:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}a{color:var(--accent)}
.brand{background:var(--accent);color:#fff;padding:10px 16px;font-weight:600;letter-spacing:.02em}
.brand .wrap{max-width:1100px;margin:0 auto;display:flex;justify-content:space-between;gap:12px}
.brand small{opacity:.8;font-weight:400;color:#fff}
h1{margin:0;font-size:1.6rem;display:flex;align-items:center;gap:10px;flex-wrap:wrap}h2{margin:0 0 10px;font-size:1.05rem}
.ball{width:16px;height:16px;border-radius:50%;flex:none;background:radial-gradient(circle at 35% 35%,#fff,#bbb 40%,#666);
box-shadow:0 0 0 3px var(--accent2)}.muted{color:var(--muted);margin:2px 0 10px}small{color:var(--muted)}.small{font-size:.85rem}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
select,button,input{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--ink)}button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}
button.ghost{background:var(--card);color:var(--accent)}button.danger{background:var(--bad);border-color:var(--bad)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}.chip{display:flex;gap:6px;align-items:center;
background:var(--card);border:1px solid var(--line);border-radius:99px;padding:4px 10px 4px 4px;text-decoration:none;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}
.card.error{border-left:4px solid var(--bad)}.card.ok{border-left:4px solid var(--ok)}
.warnbox{border-left:4px solid var(--accent2);background:color-mix(in srgb,var(--accent2) 14%,transparent);
padding:8px 12px;border-radius:6px;margin:10px 0}
.two{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px}
@media (max-width:820px){.two{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{padding:8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500;font-size:.82rem}
.num{text-align:right;font-variant-numeric:tabular-nums}tbody tr:last-child td{border-bottom:0}
.facts th{width:34%;font-size:.88rem}.row-rejected td{opacity:.75}
.tag{display:inline-block;font-size:.75rem;padding:1px 9px;border-radius:99px;background:var(--line);color:var(--ink);
font-weight:600;white-space:nowrap}.s-new{background:var(--new);color:#1d1730}.s-contacted{background:var(--contacted);color:#fff}
.s-confirmed{background:var(--ok);color:#fff}.s-declined{background:var(--muted);color:#fff}.s-rejected{background:var(--bad);color:#fff}
.stack{display:flex;flex-direction:column;gap:10px;margin:0 0 14px}.stack label{display:flex;flex-direction:column;gap:4px;
color:var(--muted);font-size:.88rem}.stack input,.stack select{color:var(--ink)}.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.hist{list-style:none;margin:0;padding:0}.hist li{padding:6px 0;border-bottom:1px solid var(--line);font-size:.92rem}
.hist li:last-child{border:0}.hist .when{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:4px}
@media (max-width:640px){table{font-size:.85rem}th,td{padding:6px 4px}}
"""


def page(title: str, body: str, login: str = "") -> str:
    who = f"<small>signed in as {h(login)}</small>" if login else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{h(title)} · Goat Arcade</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{STYLE}</style></head>
<body><div class="brand"><div class="wrap"><span>Goat Arcade · Party bookings</span>{who}</div></div>
<main>{body}</main><script src="/annex-errors.js"></script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port, venues_with_manager=len(MANAGERS))
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
