"""The console (M2.4, reshaped in M2.7-M2.9): read-only pages for builders and admins, in its own container (D69).

It trusts `Remote-User` and `Remote-Groups`, which only Traefik's forward-auth can set here: this container is on the
`platform` network (Traefik) and a network shared only with appdata-db, never on `edge` or `appdata` where apps are, and
`strip-identity` blanks anything a browser sends. Its database role may only SELECT. It has no Docker access, no SMTP, no
Slack token and no secret of any app, and no route changes anything.

  GET /status                every signed-in user: which apps and platform parts are up
  GET /                      the overview: what needs attention, the apps you own (admins: all, with the platform and the latest activity)
  GET /apps                  the apps list, filtered
  GET /apps/<app>[/tab]      summary, /logs, /errors, /releases, /usage, /config: the owner and admins
  GET /audit  (/activity)    the activity timeline: admins only
  GET /platform              the server and the platform's parts: admins only
"""
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, console_data as data, console_pages_admin as pg, console_pages_app as pa, console_pages_overview as po, console_view as v, db
from .errors import OpsError

app = FastAPI(title="annex-console", docs_url=None, redoc_url=None, openapi_url=None)
APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}


def reply(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers=HEADERS)


def signed_in(request: Request) -> v.Who | None:
    return v.parse_who(request.headers)


NOT_SIGNED_IN = PlainTextResponse("Not signed in: open the console through its address on the Annex server, which signs you in.",
                                  status_code=401, headers=HEADERS)


@app.exception_handler(StarletteHTTPException)
async def _http(request: Request, e: StarletteHTTPException):
    return PlainTextResponse("not found" if e.status_code == 404 else str(e.detail), status_code=e.status_code, headers=HEADERS)


@app.exception_handler(OpsError)
async def _ops(request: Request, e: OpsError):
    return PlainTextResponse(f"The console can't read its data right now ({e.message})", status_code=503, headers=HEADERS)


@app.get("/healthz")
def healthz():
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except OpsError as e:
        return PlainTextResponse(e.message, status_code=503)
    return PlainTextResponse("ok")


@app.get("/status")
def status(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    apps, platform = data.status_view()
    _, newest = data.health_snapshot()
    return reply(po.status_page(who, apps, platform, data.checker_stale(newest, time.time(), config.tuning().check_interval_s)))


@app.get("/")
def overview(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    return reply(po.overview_page(who, data.overview(who)))


@app.get("/apps")
def apps_list(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    return reply(po.apps_page(who, data.overview(who), request.query_params.get("f", "All")[:20], request.query_params.get("q", "")[:60]))


def authorised(who: v.Who, name: str):
    """(found app row or None, response to send instead or None). A name that isn't an app, or isn't yours, is the same
    403 for a non-admin: the console doesn't tell people which apps exist."""
    if not APP_NAME.match(name):
        return None, reply(v.not_found(who, "No such app"), 404)
    row = next((a for a in data.apps() if a["app"] == name), None)
    if row is None:
        return None, (reply(v.not_found(who, f"There is no app named {name}"), 404) if who.admin else reply(v.forbidden(who, "this app"), 403))
    if not v.can_see_app(who, row["owner"]):
        return None, reply(v.forbidden(who, "this app"), 403)
    return row, None


def _guarded(request: Request, name: str):
    """(who, core data of the app, response to send instead)."""
    who = signed_in(request)
    if who is None:
        return None, None, NOT_SIGNED_IN
    row, resp = authorised(who, name)
    if resp is not None:
        return who, None, resp
    return who, data.app_core(who, name), None


@app.get("/apps/{name}")
def app_page(name: str, request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    row, resp = authorised(who, name)
    if resp is not None:
        return resp
    d = data.app_detail(who, name)
    return reply(pa.summary_page(who, d, data.app_attention(d, datetime.now(timezone.utc))))


@app.get("/apps/{name}/logs")
def logs(name: str, request: Request):
    who, d, resp = _guarded(request, name)
    if resp is not None:
        return resp
    q = {k: request.query_params.get(k, "").strip()[:200] for k in ("level", "release", "grep", "request", "since", "source")}
    before = None
    if request.query_params.get("before"):
        try:
            before = datetime.fromisoformat(request.query_params["before"].replace("Z", "+00:00"))
        except ValueError:
            before = None
    rows = data.log_lines(name, q, before)
    older = None
    if len(rows) >= 100:
        params = {k: x for k, x in {**q, "before": rows[0]["ts"]}.items() if x}
        older = f"/apps/{name}/logs?{urlencode(params)}"
    return reply(pa.logs_page(who, d, rows, q, older))


@app.get("/apps/{name}/errors")
def errors_tab(name: str, request: Request):
    who, d, resp = _guarded(request, name)
    if resp is not None:
        return resp
    return reply(pa.errors_page(who, d, request.query_params.get("s", "active")[:10]))


@app.get("/apps/{name}/releases")
def releases_tab(name: str, request: Request):
    who, d, resp = _guarded(request, name)
    if resp is not None:
        return resp
    return reply(pa.releases_page(who, d, show_all=request.query_params.get("all") == "1"))


@app.get("/apps/{name}/usage")
def usage_tab(name: str, request: Request):
    who, d, resp = _guarded(request, name)
    if resp is not None:
        return resp
    return reply(pa.usage_page(who, {**d, **data.usage_part(who, name)}))


@app.get("/apps/{name}/config")
def config_tab(name: str, request: Request):
    who, d, resp = _guarded(request, name)
    if resp is not None:
        return resp
    return reply(pa.config_page(who, d))


@app.get("/audit")
@app.get("/activity")
def audit_page(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    if not who.admin:
        return reply(v.forbidden(who, "the activity timeline"), 403)
    q = {k: request.query_params.get(k, "").strip()[:100] for k in ("app", "chip", "actor", "since", "before")}
    chip = q["chip"] if q["chip"] in data.CHIPS else "All"
    q["chip"] = chip if chip != "All" else ""
    before = None
    if q["before"]:
        try:
            before = datetime.fromisoformat(q["before"].replace("Z", "+00:00"))
        except ValueError:
            q["before"] = ""
    secs = pg.SINCE.get(q["since"])
    events, more = data.activity(pg.PAGE, chip, q["app"] or None, q["actor"] or None, secs, before)
    return reply(pg.activity_page(who, events, more, q, list(data.CHIPS)))


@app.get("/platform")
def platform(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    if not who.admin:
        return reply(v.forbidden(who, "the platform page"), 403)
    return reply(pg.platform_page(who, data.platform_view()))
