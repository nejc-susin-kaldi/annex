"""league-desk: Goat Arcade's tournament desk. An Annex web tool (FastAPI).

Tournaments, standings and players come read-only from the gateway. Sign-ups taken at the desk, which the club
system doesn't have yet, are the app's own data: they live in its Postgres (store.py) and post one line to Slack.
"""
import html
import os
import threading
import time
from datetime import date, timedelta
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

import desk
import store
from annex_runtime import (CsrfMiddleware, GatewayError, NotifyError, browser_errors_js, current_user, gateway, log,
                           post_slack, report_browser_error, request_context)

SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#ga-community")
TOP_N = 8
CACHE_SECONDS = 60

app = FastAPI(title="league-desk")
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


# --- gateway reads (cached briefly: the club data changes at most daily) ------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def cached(key: str, load, seconds: int = CACHE_SECONDS):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
    t0 = time.monotonic()
    value = load()
    log.debug("gateway read", key=key, ms=round((time.monotonic() - t0) * 1000))
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def tournaments() -> list[dict]:
    return cached("tournaments", lambda: gateway(
        "tournaments", select="tournament_id,name,venue_code,venue_name,starts_on,format,status,entry_fee_eur,"
                              "max_players,entries,winner_handle,winner_nickname", order="starts_on.desc"))


def official_entries(tournament_id: int, fresh: bool = False) -> list[dict]:
    """The club system's entries for one tournament (tournament_entries). `fresh` skips the cache: the sign-up check
    must not miss an entry made in the last minute."""
    def load():
        return gateway("tournament_entries", select="nickname,handle,seed,entered_at",
                       tournament_id=f"eq.{tournament_id}", order="nickname.asc", limit=500)
    return load() if fresh else cached(f"entries:{tournament_id}", load)


def venue_names() -> dict[str, str]:
    rows = cached("venues", lambda: gateway("venues", select="venue_code,venue_name,city"), 600)
    return {v["venue_code"]: f"{v['venue_name']} ({v['city']})" for v in rows}


def data_last_day() -> date | None:
    """The last day with play in the data (the latest member game): 'in N days' counts from it, not the wall clock."""
    rows = cached("last_day", lambda: gateway("player_profiles", select="last_played_at",
                                              order="last_played_at.desc.nullslast", limit=1), 600)
    return desk.as_date(rows[0]["last_played_at"]) if rows and rows[0]["last_played_at"] else None


def find_player(nick: str) -> dict | None:
    """The member with exactly this nickname (any case), or None."""
    rows = gateway("player_profiles", select="nickname,handle,rating,home_venue_code", nickname=f"ilike.{nick}",
                   limit=5)
    return desk.pick_exact(rows, nick)


# --- page parts ---------------------------------------------------------------------------------------------------
def esc(v) -> str:
    return html.escape(str(v))


def fmt_day(v) -> str:
    return f"{desk.as_date(v):%a %-d %b %Y}"


def fmt_fmt(f: str) -> str:
    return {"match_play": "match play", "best_game": "best game (EM tables)"}.get(f, f)


def box(kind: str, text_html: str) -> str:
    return f'<div class="note {kind}">{text_html}</div>'


def gateway_down(login: str, step: str, e: GatewayError) -> HTMLResponse:
    log.error("gateway read failed", step=step, status=e.status)
    return HTMLResponse(page("League desk", box("bad", "The club data can't be read right now. Try again in a minute."),
                             login), 502)


def signup_form(t: dict, nickname: str = "", compact: bool = False) -> str:
    return (f'<form method="post" action="/signups" class="signup{" compact" if compact else ""}">'
            f'<input type="hidden" name="tournament_id" value="{t["tournament_id"]}">'
            + ("" if compact else '<label for="nick">Player nickname</label>')
            + (f'<input type="hidden" name="nickname" value="{esc(nickname)}">' if compact else
               f'<input id="nick" name="nickname" value="{esc(nickname)}" placeholder="e.g. otter12" required '
               f'autocomplete="off" spellcheck="false" maxlength="40">')
            + f'<button>Sign up{" for " + esc(t["name"]) if compact else ""}</button></form>')


def upcoming_card(t: dict, pend: list[dict], entrants: list[str] | None, last_day: date | None, nickname: str, venues: dict) -> str:
    starts = desk.as_date(t["starts_on"])
    official, n_pend, cap = t["entries"] or 0, len(pend), t["max_players"] or 0
    left = desk.places_left(cap, official, n_pend)
    when = ""
    if last_day:
        # "today" is the day after the data's last day (the data ends the day before it was loaded)
        d = desk.days_between(last_day + timedelta(days=1), starts)
        when = (f'<span class="pill" title="counted from the day after the data ends">in {d} day{"s" if d != 1 else ""}'
                f'</span>' if d > 0 else '<span class="pill">today</span>' if d == 0 else
                '<span class="pill">date passed, results not in yet</span>')
    pct_o = 100 * official / cap if cap else 0
    pct_p = 100 * n_pend / cap if cap else 0
    bar = (f'<div class="fill" role="img" aria-label="{official} entered, {n_pend} pending, {left} free of {cap}">'
           f'<span class="o" style="width:{pct_o:.1f}%"></span><span class="p" style="width:{pct_p:.1f}%"></span></div>')
    if pend:
        rows = "".join(
            f'<tr><td><a href="/players/{quote(s["nickname"])}">{esc(s["nickname"])}</a></td>'
            f'<td>{esc(s["signed_up_by"])}</td><td>{s["local_at"]:%-d %b, %H:%M}</td>'
            f'<td class="num"><form method="post" action="/signups/{s["id"]}/withdraw">'
            f'<button class="ghost">Withdraw</button></form></td></tr>' for s in pend)
        pend_html = (f'<table><thead><tr><th>Nickname</th><th>Signed up by</th><th>When</th><th></th></tr></thead>'
                     f'<tbody>{rows}</tbody></table>')
    else:
        pend_html = '<p class="muted">No desk sign-ups waiting for the club system.</p>'
    if entrants is None:
        entered_html = '<p class="muted">The club system\'s entry list can\'t be read right now.</p>'
    elif entrants:
        entered_html = ('<p class="entrants">' + ", ".join(f'<a href="/players/{quote(n)}">{esc(n)}</a>' for n in entrants)
                        + '</p>')
    else:
        entered_html = '<p class="muted">Nobody is entered in the club system yet.</p>'
    form = signup_form(t, nickname) if left > 0 else box("warn", "The tournament is full: no places left.")
    return f"""
<section class="card hero">
  <div class="hero-top">
    <div><span class="kicker">Next tournament</span><h2>{esc(t['name'])}</h2>
    <p class="muted">{fmt_day(starts)} · {esc(venues.get(t['venue_code'], t['venue_name']))} · {fmt_fmt(t['format'])}
    · entry €{t['entry_fee_eur']:.0f}</p></div>{when}
  </div>
  <div class="stats">
    <div><b>{cap}</b><span>places</span></div>
    <div><b>{official}</b><span>entered (club system)</span></div>
    <div><b>{n_pend}</b><span>pending at the desk</span></div>
    <div class="left"><b>{left}</b><span>places left</span></div>
  </div>
  {bar}
  <div class="cols">
    <div><h3>Sign a player up</h3>{form}
      <p class="small muted">The nickname must be a member's, and not already entered in the club system.</p></div>
    <div><h3>Pending desk sign-ups</h3>{pend_html}
      <h3>Entered in the club system ({official})</h3>{entered_html}</div>
  </div>
</section>"""


def past_table(past: list[dict]) -> str:
    if not past:
        return '<p class="muted">No completed tournaments yet.</p>'
    rows = "".join(
        f'<tr><td><a href="/tournaments/{t["tournament_id"]}">{esc(t["name"])}</a></td><td>{fmt_day(t["starts_on"])}</td>'
        f'<td>{esc(t["venue_name"])}</td><td class="num">{t["entries"] or 0}</td>'
        f'<td>{winner(t)}</td></tr>' for t in past)
    return (f'<table><thead><tr><th>Tournament</th><th>Date</th><th>Venue</th><th class="num">Players</th>'
            f'<th>Winner</th></tr></thead><tbody>{rows}</tbody></table>')


def winner(t: dict) -> str:
    if not t.get("winner_nickname"):
        return '<span class="muted">–</span>'
    return (f'<span class="trophy">&#9733;</span> <a href="/players/{quote(t["winner_nickname"])}">'
            f'{esc(t["winner_nickname"])}</a> <small>{esc(t["winner_handle"] or "")}</small>')


# --- the desk -----------------------------------------------------------------------------------------------------
def render_index(login: str, notice: str = "", nickname: str = "", status: int = 200) -> HTMLResponse:
    try:
        ts = tournaments()
        venues = venue_names()
        last_day = data_last_day()
    except GatewayError as e:
        return gateway_down(login, "load desk", e)
    upcoming, past = desk.split_tournaments(ts)
    parts = [notice]
    for t in upcoming:
        try:
            pend = store.pending(t["tournament_id"])
        except Exception:
            log.exception("could not read sign-ups", tournament_id=t["tournament_id"])
            pend = []
            parts.append(box("bad", "Desk sign-ups can't be read right now; the official entries are shown."))
        try:
            entrants = desk.official_nicknames(official_entries(t["tournament_id"]))
        except GatewayError as e:
            log.warn("could not read entries", tournament_id=t["tournament_id"], status=e.status)
            entrants = None
        parts.append(upcoming_card(t, pend, entrants, last_day, nickname, venues))
    if not upcoming:
        parts.append('<section class="card"><h2>Next tournament</h2><p class="muted">No tournament is open for '
                     'registration.</p></section>')
    span = ""
    if ts:
        days = sorted(desk.as_date(t["starts_on"]) for t in ts)
        span = f" · tournaments {days[0]:%-d %b %Y} to {days[-1]:%-d %b %Y}"
    data_note = f"Club data up to {last_day:%a %-d %b %Y}{span}." if last_day else "No play data yet."
    parts.append(f'<section class="card"><h2>Past tournaments</h2><p class="muted">Winners and the number of '
                 f'players; open one for its standings.</p>{past_table(past)}</section>'
                 f'<p class="range">{data_note}</p>')
    return HTMLResponse(page("League desk", header("Tournament desk", "Sign-ups, standings and players for Goat "
                                                   "Arcade's pinball tournaments.") + "".join(parts), login), status)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, signed: int | None = None, withdrawn: int | None = None, posted: int = 1):
    user = current_user(request.headers)
    notice = ""
    if signed or withdrawn:
        try:
            s = store.get(signed or withdrawn)
        except Exception:
            log.exception("could not read sign-up", signup_id=signed or withdrawn)
            s = None
        if s and signed and not s["withdrawn_at"]:
            notice = box("good", f"<b>{esc(s['nickname'])}</b> is signed up at the desk, pending in the club system."
                                 + ("" if posted else f" The note to {esc(SLACK_CHANNEL)} couldn't be posted: "
                                                      "tell the community channel yourself."))
        elif s and withdrawn and s["withdrawn_at"]:
            notice = box("good", f"<b>{esc(s['nickname'])}</b>'s desk sign-up is withdrawn.")
    return render_index(user.login, notice)


@app.post("/signups")
def sign_up(request: Request, nickname: str = Form(""), tournament_id: int = Form(...)):
    user = current_user(request.headers)
    nick = desk.clean_nickname(nickname)
    if not nick:
        log.info("sign-up refused", reason="bad nickname", tournament_id=tournament_id)
        return render_index(user.login, box("bad", "Type a member's nickname: letters, digits, '.', '_' or '-', "
                                                   "2 to 40 characters."), nickname, 400)
    try:
        t = next((x for x in tournaments() if x["tournament_id"] == tournament_id), None)
        player = find_player(nick)
        entries = (official_entries(tournament_id, fresh=True)
                   if t and t["status"] == "registration" and player else [])
    except GatewayError as e:
        return gateway_down(user.login, "sign up", e)
    if not t or t["status"] != "registration":
        log.info("sign-up refused", reason="not open", tournament_id=tournament_id)
        return render_index(user.login, box("bad", "That tournament isn't open for registration."), nick, 409)
    if not player:
        log.info("sign-up refused", reason="unknown nickname", tournament_id=tournament_id)
        return render_index(user.login, box(
            "bad", f"There's no member with the nickname <b>{esc(nick)}</b>. Check the spelling, or "
                   f"<a href=\"/players?q={quote(nick)}\">search the players</a>. New players need a member card "
                   f"first."), nick, 404)
    nick = player["nickname"]  # the club system's spelling
    if desk.is_official(entries, nick):
        log.info("sign-up refused", reason="already entered", tournament_id=tournament_id)
        return render_index(user.login, box("warn", f"<b>{esc(nick)}</b> is already entered in the club system for "
                                                    f"{esc(t['name'])}."), "", 409)
    free = desk.places_left(t["max_players"], t["entries"], 0)
    try:
        signup_id, n_pend = store.add(tournament_id, nick, user.login, free)
    except store.Duplicate:
        log.info("sign-up refused", reason="duplicate", tournament_id=tournament_id)
        return render_index(user.login, box("warn", f"<b>{esc(nick)}</b> is already signed up at the desk for "
                                                    f"{esc(t['name'])}."), "", 409)
    except store.Full:
        log.info("sign-up refused", reason="full", tournament_id=tournament_id)
        return render_index(user.login, box("bad", f"{esc(t['name'])} is full: no places left."), "", 409)
    left = desk.places_left(t["max_players"], t["entries"], n_pend)
    log.info("signed up", signup_id=signup_id, tournament_id=tournament_id, places_left=left)
    posted = announce(desk.slack_line(nick, player.get("rating"), player.get("home_venue_code"), t, user.login, left),
                      signup_id)
    return RedirectResponse(f"/?signed={signup_id}" + ("" if posted else "&posted=0"), status_code=303)


def announce(text: str, signup_id: int) -> bool:
    """One line to #ga-community. A failed post doesn't undo the sign-up: it's logged and the desk carries on."""
    try:
        r = post_slack(SLACK_CHANNEL, text)
        log.info("slack posted", channel=SLACK_CHANNEL, signup_id=signup_id, slack_id=r.get("id"))
        return True
    except NotifyError as e:
        log.warn("slack post failed", channel=SLACK_CHANNEL, signup_id=signup_id, status=e.status, error=str(e))
        return False


@app.post("/signups/{signup_id}/withdraw")
def withdraw(request: Request, signup_id: int):
    user = current_user(request.headers)
    row = store.withdraw(signup_id, user.login)
    if not row:
        log.info("withdraw refused", signup_id=signup_id, reason="not pending")
        return render_index(user.login, box("warn", "That sign-up was already withdrawn."), "", 409)
    log.info("sign-up withdrawn", signup_id=signup_id, tournament_id=row["tournament_id"])
    return RedirectResponse(f"/?withdrawn={signup_id}", status_code=303)


# --- standings ----------------------------------------------------------------------------------------------------
@app.get("/tournaments/{tournament_id}", response_class=HTMLResponse)
def standings(request: Request, tournament_id: int, show_all: int = Query(0, alias="all")):
    user = current_user(request.headers)
    try:
        t = next((x for x in tournaments() if x["tournament_id"] == tournament_id), None)
        if t is None:
            return HTMLResponse(page("League desk", header("No such tournament") +
                                     box("bad", 'No tournament with that id. <a href="/">Back to the desk</a>.'),
                                     user.login), 404)
        if t["status"] != "completed":
            return RedirectResponse("/", status_code=303)
        params = {"select": "position,handle,nickname,points,games_played,best_score",
                  "tournament_id": f"eq.{tournament_id}", "order": "position,nickname"}
        if not show_all:
            params["limit"] = TOP_N
        rows = cached(f"standings:{tournament_id}:{show_all}", lambda: gateway("tournament_standings", **params), 600)
    except GatewayError as e:
        return gateway_down(user.login, "standings", e)
    field = t["entries"] or len(rows)
    body_rows = "".join(
        f'<tr class="{"podium p" + str(r["position"]) if r["position"] <= 3 else ""}">'
        f'<td class="num pos">{r["position"]}</td><td><a href="/players/{quote(r["nickname"])}">{esc(r["nickname"])}</a>'
        f' <small>{esc(r["handle"])}</small></td><td class="num">{r["points"]}</td>'
        f'<td class="num">{r["games_played"]}</td><td class="num">{r["best_score"]:,}</td></tr>' for r in rows)
    toggle = (f'<a href="/tournaments/{tournament_id}?all=1">Show all {field} players</a>' if not show_all and field > len(rows)
              else f'<a href="/tournaments/{tournament_id}">Top {TOP_N} only</a>' if show_all else "")
    body = header(t["name"], f"{fmt_day(t['starts_on'])} · {esc(t['venue_name'])} · {fmt_fmt(t['format'])} · "
                             f"{field} players", back=True) + f"""
<section class="card"><h2>{"Final standings" if show_all else f"Top {min(TOP_N, len(rows))}"}</h2>
<p class="muted">Points over four rounds (7-5-3-1 per group); ties split by best single score.
Scores compare only within one tournament's machines.</p>
<table><thead><tr><th class="num">#</th><th>Player</th><th class="num">Points</th><th class="num">Games</th>
<th class="num">Best score</th></tr></thead><tbody>{body_rows}</tbody></table>
<p class="toggle">{toggle}</p></section>"""
    log.info("standings shown", tournament_id=tournament_id, rows=len(rows))
    return HTMLResponse(page(f"League desk · {t['name']}", body, user.login))


# --- players ------------------------------------------------------------------------------------------------------
def search_form(q: str = "") -> str:
    return (f'<form method="get" action="/players" class="search"><label for="q">Nickname</label>'
            f'<input id="q" name="q" value="{esc(q)}" placeholder="part of a nickname" autocomplete="off" '
            f'spellcheck="false"><button>Search</button></form>')


@app.get("/players", response_class=HTMLResponse)
def players(request: Request, q: str = ""):
    user = current_user(request.headers)
    term = desk.search_term(q)
    results = ""
    if q and not term:
        results = box("warn", "Nicknames are letters, digits, '.', '_' and '-'.")
    elif term:
        try:
            rows = gateway("player_profiles", select="nickname,handle,rating,home_venue_code,games_180d",
                           nickname=f"ilike.*{term}*", order="rating.desc", limit=25)
        except GatewayError as e:
            return gateway_down(user.login, "player search", e)
        exact = desk.pick_exact(rows, term)
        if exact and len(rows) == 1:
            return RedirectResponse(f"/players/{quote(exact['nickname'])}", status_code=303)
        log.info("player search", matches=len(rows))
        if rows:
            results = ("<table><thead><tr><th>Nickname</th><th>Initials</th><th class='num'>Rating</th>"
                       "<th>Home</th><th class='num'>Games (180 d)</th></tr></thead><tbody>" + "".join(
                           f'<tr><td><a href="/players/{quote(r["nickname"])}">{esc(r["nickname"])}</a></td>'
                           f'<td>{esc(r["handle"])}</td><td class="num">{r["rating"]}</td>'
                           f'<td>{esc(r["home_venue_code"])}</td><td class="num">{r["games_180d"]:,}</td></tr>'
                           for r in rows) + "</tbody></table>"
                       + ('<p class="muted small">The first 25 by rating; type more of the nickname to narrow it.</p>'
                          if len(rows) == 25 else ""))
        else:
            results = box("warn", f"No member's nickname contains <b>{esc(term)}</b>.")
    body = header("Players", "Look a member up by nickname: rating, home venue, leaderboard spots, past placings.",
                  back=True) + f'<section class="card">{search_form(q)}{results}</section>'
    return HTMLResponse(page("League desk · Players", body, user.login))


@app.get("/players/{nickname}", response_class=HTMLResponse)
def player(request: Request, nickname: str):
    user = current_user(request.headers)
    nick = desk.clean_nickname(nickname)
    try:
        p = None
        if nick:
            rows = gateway("player_profiles", select="nickname,handle,home_venue_code,member_since,rating,games_180d,"
                                                     "venues_played,last_played_at,leaderboard_spots,best_rank",
                           nickname=f"ilike.{nick}", limit=5)
            p = desk.pick_exact(rows, nick)
        if not p:
            body = header("Players", back=True) + f'<section class="card">{search_form(nickname)}' + box(
                "bad", f"There's no member with the nickname <b>{esc(nickname)}</b>.") + "</section>"
            return HTMLResponse(page("League desk · Players", body, user.login), 404)
        nick = p["nickname"]
        spots = gateway("leaderboard", select="title,rank,score,venue_code,played_at", nickname=f"eq.{nick}",
                        order="rank,title")
        placings = gateway("tournament_standings", select="tournament_id,tournament_name,starts_on,position,points",
                           nickname=f"eq.{nick}", order="starts_on.desc")
        ts = {t["tournament_id"]: t for t in tournaments()}
        venues = venue_names()
    except GatewayError as e:
        return gateway_down(user.login, "player", e)
    log.info("player shown", spots=len(spots), placings=len(placings))
    upcoming, _ = desk.split_tournaments(list(ts.values()))
    pend_for = set()
    try:
        for t in upcoming:
            if any(s["nickname"].lower() == nick.lower() for s in store.pending(t["tournament_id"])):
                pend_for.add(t["tournament_id"])
    except Exception:
        log.exception("could not read sign-ups", step="player")

    home = p["home_venue_code"]
    stats = f"""
<div class="stats">
  <div><b>{p['rating']}</b><span>rating</span></div>
  <div><b>{p['games_180d']:,}</b><span>games, last 180 days</span></div>
  <div><b>{p['leaderboard_spots']}</b><span>leaderboard spots{f" · best #{p['best_rank']}" if p['best_rank'] else ""}</span></div>
  <div><b>{p['venues_played']}</b><span>venues played</span></div>
</div>"""
    if spots:
        def spot_table(rs):
            return ('<table><thead><tr><th class="num">Rank</th><th>Machine</th><th class="num">Score</th>'
                    '<th>Venue</th><th>Set on</th></tr></thead><tbody>' + "".join(
                        f'<tr><td class="num pos">{r["rank"]}</td><td>{esc(r["title"])}</td>'
                        f'<td class="num">{r["score"]:,}</td><td>{esc(r["venue_code"])}</td>'
                        f'<td>{fmt_day(r["played_at"])}</td></tr>' for r in rs) + '</tbody></table>')
        spots_html = spot_table(spots[:10])
        if len(spots) > 10:
            spots_html += (f'<details><summary>{len(spots) - 10} more</summary>{spot_table(spots[10:])}</details>')
    else:
        spots_html = '<p class="muted">Not in any machine\'s top 10.</p>'
    if placings:
        pl_rows = "".join(
            f'<tr><td><a href="/tournaments/{r["tournament_id"]}">{esc(r["tournament_name"])}</a></td>'
            f'<td>{fmt_day(r["starts_on"])}</td><td>{"<span class=trophy>&#9733;</span> " if r["position"] == 1 else ""}'
            f'{desk.placing_label(r["position"], (ts.get(r["tournament_id"]) or {}).get("entries"))}</td>'
            f'<td class="num">{r["points"]}</td></tr>' for r in placings)
        pl_html = ('<table><thead><tr><th>Tournament</th><th>Date</th><th>Placing</th><th class="num">Points</th>'
                   f'</tr></thead><tbody>{pl_rows}</tbody></table>')
    else:
        pl_html = '<p class="muted">No completed tournaments played.</p>'
    actions = "".join(
        (f'<p class="muted">Signed up at the desk for {esc(t["name"])}, pending.</p>' if t["tournament_id"] in pend_for
         else signup_form(t, nick, compact=True)) for t in upcoming)
    last = f" · last played {fmt_day(p['last_played_at'])}" if p.get("last_played_at") else ""
    body = header(nick, f"Initials {esc(p['handle'])} · home venue {esc(venues.get(home, home))} · member since "
                        f"{fmt_day(p['member_since'])}{last}", back=True) + f"""
<section class="card">{stats}{actions}</section>
<section class="two">
  <div class="card"><h2>Leaderboard spots</h2>{spots_html}</div>
  <div class="card"><h2>Tournament placings</h2>{pl_html}</div>
</section>
<section class="card">{search_form()}</section>"""
    return HTMLResponse(page(f"League desk · {nick}", body, user.login))


# --- the shell ----------------------------------------------------------------------------------------------------
def header(title: str, sub: str = "", back: bool = False) -> str:
    nav = '<a href="/">&larr; Desk</a>' if back else '<a href="/players">Player lookup &rarr;</a>'
    return (f'<header class="top"><div><h1><span class="ball"></span>{esc(title)}</h1>'
            + (f'<p class="muted">{sub}</p>' if sub else "") + f'</div><nav>{nav}</nav></header>')


STYLE = """
:root{--bg:#f5f3f0;--card:#fff;--ink:#1c1a24;--muted:#6b6676;--line:#e7e2dc;--accent:#0e6b5c;--accent2:#f2a900;
--good:#1b7f4b;--bad:#b3261e;--warn:#9a6700;--pend:#f2c94c;--gold:#d4a017}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--card:#1b1e27;--ink:#ebe9f0;--muted:#a19cab;--line:#2c3040;
--accent:#46b29d;--accent2:#f2b632;--good:#5fd08f;--bad:#ff8a80;--warn:#f2c94c;--pend:#b8952e;--gold:#f2c94c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1060px;margin:0 auto;padding:20px 16px 48px}a{color:var(--accent)}
.brand{background:var(--accent);color:#fff;padding:10px 16px;font-weight:600;letter-spacing:.02em}
.brand .wrap{max-width:1060px;margin:0 auto;display:flex;justify-content:space-between;gap:12px}
.brand small{color:#fff;opacity:.8;font-weight:400}.brand a{color:#fff;text-decoration:none}
h1{margin:0;font-size:1.7rem;display:flex;align-items:center;gap:10px}h2{margin:0 0 6px;font-size:1.15rem}
h3{margin:0 0 8px;font-size:1rem}.ball{width:16px;height:16px;border-radius:50%;flex:none;
background:radial-gradient(circle at 35% 35%,#fff,#bbb 40%,#666);box-shadow:0 0 0 3px var(--accent2)}
.muted{color:var(--muted);margin:2px 0 10px}small{color:var(--muted)}.small{font-size:.85rem}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.hero{border-top:4px solid var(--accent2)}.hero-top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.kicker{color:var(--accent);font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.hero h2{font-size:1.5rem;margin:2px 0}.pill{align-self:flex-start;background:var(--line);border-radius:99px;
padding:3px 10px;font-size:.85rem;white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:14px 0 10px}
.stats div{display:flex;flex-direction:column}.stats b{font-size:1.8rem;font-variant-numeric:tabular-nums}
.stats span{color:var(--muted);font-size:.85rem}.stats .left b{color:var(--accent)}
.fill{display:flex;height:10px;border-radius:99px;background:var(--line);overflow:hidden;margin-bottom:16px}
.fill .o{background:var(--accent)}.fill .p{background:var(--pend)}
.cols{display:grid;grid-template-columns:minmax(260px,1fr) minmax(300px,1.4fr);gap:24px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
form.signup{display:flex;flex-wrap:wrap;gap:8px;align-items:center}form.signup label{width:100%;color:var(--muted);
font-size:.85rem}form.signup.compact{margin-top:8px}form.search{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
margin-bottom:12px}form.search label{color:var(--muted)}
input,button{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--ink)}input{min-width:200px;flex:1}button{background:var(--accent);color:#fff;border-color:var(--accent);
cursor:pointer}button.ghost{background:transparent;color:var(--bad);border-color:var(--line);padding:3px 10px;
font-size:.85rem}td form{margin:0}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{padding:7px 8px;border-bottom:1px solid var(--line);
text-align:left}th{color:var(--muted);font-weight:500;font-size:.82rem}.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{font-weight:600;width:3em}.podium.p1 .pos{color:var(--gold)}.trophy{color:var(--gold)}
.note{border-radius:8px;padding:10px 14px;margin:0 0 16px;border:1px solid var(--line);border-left-width:4px;
background:var(--card)}.note.good{border-left-color:var(--good)}.note.bad{border-left-color:var(--bad)}
.note.warn{border-left-color:var(--warn)}.cols .note{margin-top:8px}
.range{color:var(--muted);font-size:.9rem}.toggle{margin:10px 0 0}
@media (max-width:720px){.cols{grid-template-columns:1fr}table{font-size:.85rem}th,td{padding:6px 4px}
.stats b{font-size:1.4rem}}
"""


def page(title: str, body: str, login: str = "") -> str:
    who = f"<small>signed in as {esc(login)}</small>" if login else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{STYLE}</style></head>
<body><div class="brand"><div class="wrap"><a href="/">Goat Arcade · League desk</a>{who}</div></div>
<main>{body}</main><script src="/annex-errors.js"></script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("started", port=port, slack_channel=SLACK_CHANNEL)
    # log_config=None and access_log=False: uvicorn's own lines would be plain text; request_log above does it
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=None, access_log=False)
