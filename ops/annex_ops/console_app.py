"""The console (M2.4): read-only pages for builders and admins, in its own container (D69).

It trusts `Remote-User` and `Remote-Groups`, which only Traefik's forward-auth can set here: this container is on the
`platform` network (Traefik) and a network shared only with appdata-db, never on `edge` or `appdata` where apps are, and
`strip-identity` blanks anything a browser sends. Its database role may only SELECT. It has no Docker access, no SMTP, no
Slack token and no secret of any app, and no route changes anything.

  GET /status            every signed-in user: which apps and platform parts are up
  GET /                  the apps you own (admins: all, with flags and the platform)
  GET /apps/<app>        status, releases, usage, errors, warnings, config: the owner and admins
  GET /apps/<app>/logs   search the stored logs: the owner and admins
  GET /audit             the audit trail: admins only
"""
import re
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import audit, console_data as data, console_view as v, db
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
    return reply(v.status_page(who, apps, platform))


@app.get("/")
def fleet(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    f = data.fleet(who)
    return reply(v.fleet_page(who, f["rows"], f["platform"], f["host"]))


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


@app.get("/apps/{name}")
def app_page(name: str, request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    row, resp = authorised(who, name)
    if resp is not None:
        return resp
    return reply(v.app_page(who, data.app_detail(who, name)))


@app.get("/apps/{name}/logs")
def logs(name: str, request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    row, resp = authorised(who, name)
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
        from urllib.parse import urlencode
        older = f"/apps/{name}/logs?{urlencode(params)}"
    return reply(v.logs_page(who, name, rows, q, older))


@app.get("/audit")
def audit_page(request: Request):
    who = signed_in(request)
    if who is None:
        return NOT_SIGNED_IN
    if not who.admin:
        return reply(v.forbidden(who, "the audit trail"), 403)
    q = {k: request.query_params.get(k, "").strip()[:100] for k in ("app", "action", "actor", "since")}
    secs = {"1h": 3600, "1d": 86400, "7d": 604800, "30d": 2592000, "365d": 31536000}.get(q["since"])
    rows = audit.timeline(q["app"] or None, secs, q["action"] or None, q["actor"] or None, 300)
    return reply(v.audit_page(who, rows, q))
