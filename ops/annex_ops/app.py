"""The annex-ops HTTP API (apps reach it as ANNEX_OPS_URL on the edge network)."""
import hmac
import json
import sys
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (auth, config, db, email, engine, hooks_config, hooks_store, hooks_verify, incidents, sends, slack,
               usage)
from .ratelimit import SlidingWindow
from .errors import OpsError



@asynccontextmanager
async def lifespan(_: FastAPI):
    # the alert engine, the Docker watcher and the log followers; tests turn them off with OPS_WATCHERS=0
    if config.WATCHERS:
        incidents.start_background()
    yield
    incidents.stop_background()


app = FastAPI(title="annex-ops", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def is_hooks_host(request: Request) -> bool:
    """Is this request for the public webhooks hostname? That host has no login in front of it, so nothing but /h/
    is served there and every answer is terse (details go to the log and `annex hooks`)."""
    host = request.headers.get("host", "").split(":")[0].lower()
    return bool(config.HOST_HOOKS) and host == config.HOST_HOOKS


@app.middleware("http")
async def hooks_host_guard(request: Request, call_next):
    """Second layer behind Traefik's router: even if a routing mistake sent other paths here, they'd be a 404."""
    if is_hooks_host(request) and not request.url.path.startswith("/h/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return await call_next(request)


@app.exception_handler(OpsError)
async def _ops_error(request: Request, e: OpsError):
    if is_hooks_host(request):
        return error(e.status, "unavailable" if e.status >= 500 else "request refused")
    return error(e.status, e.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, e: StarletteHTTPException):
    if is_hooks_host(request):
        return error(e.status_code, "not found" if e.status_code == 404 else "request refused")
    if e.status_code == 404:
        return error(404, f"no such endpoint: {request.method} {request.url.path}. "
                          "Endpoints: GET /v1/me, POST /v1/email, POST /v1/slack")
    if e.status_code == 405:
        return error(405, f"{request.method} isn't allowed on {request.url.path}. Use POST for /v1/email and /v1/slack")
    return error(e.status_code, str(e.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, e: RequestValidationError):
    if is_hooks_host(request):
        return error(400, "bad request")
    problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'] if p != 'body')}: {err['msg']}" for err in e.errors())
    return error(400, f"invalid request ({problems}). See the endpoint's JSON in annex contract")


@app.exception_handler(Exception)
async def _unexpected(request: Request, e: Exception):
    traceback.print_exception(e, file=sys.stderr)
    if is_hooks_host(request):
        return error(500, "error")
    return error(500, "annex-ops hit an internal error. Try again; if it keeps failing, tell the platform team "
                      "(docker logs annex-ops-1)")


async def json_body(request: Request, app_name: str, channel: str) -> object:
    """The parsed JSON body. A body that can't be parsed is still an attempt, so it's logged (without content)."""
    try:
        return await _json_body(request)
    except OpsError as e:
        await run_in_threadpool(sends.log_failure, app_name, channel, [], None, e)
        raise


async def _json_body(request: Request) -> object:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.MAX_BODY_BYTES:
        raise OpsError(413, f"the request is over {config.MAX_BODY_BYTES // 1024} KB. Send less text; "
                            "link to large content instead of including it")
    body = await request.body()
    if len(body) > config.MAX_BODY_BYTES:
        raise OpsError(413, f"the request is over {config.MAX_BODY_BYTES // 1024} KB. Send less text; "
                            "link to large content instead of including it")
    try:
        return json.loads(body)
    except ValueError:
        raise OpsError(400, "the body must be JSON, with Content-Type: application/json") from None


@app.get("/healthz")
def healthz():
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except OpsError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=503)
    return {"ok": True}


@app.get("/v1/me")
async def me(request: Request):
    grant = await run_in_threadpool(auth.authenticate, request.headers.get("authorization"))
    return {"app": grant.app, "email": grant.email, "slack_channels": grant.slack_channels,
            "slack_connected": config.slack_connected()}


@app.get("/tiles/{app_name}")
async def tile(app_name: str):
    """The numbers on an app's discovery tile (Homepage calls this from its server): totals and status, never
    logins. No token: Homepage can't send one, and apps on the edge network can read these totals too (D46)."""
    return await run_in_threadpool(usage.tile_for, app_name)


@app.post("/internal/gatus")
async def gatus_webhook(request: Request):
    """Gatus's alert webhook (TRIGGERED or RESOLVED). Not for apps: it takes the internal token, not an app token."""
    token = auth.bearer_token(request.headers.get("authorization"))
    if not config.INTERNAL_TOKEN or not token or not hmac.compare_digest(token, config.INTERNAL_TOKEN):
        raise OpsError(401, "this endpoint is for Gatus only (it needs OPS_INTERNAL_TOKEN)")
    q = request.query_params
    subject = incidents.gatus_subject(q.get("group", ""), q.get("endpoint", ""))
    state = q.get("state", "").upper()
    if not subject or state not in ("TRIGGERED", "RESOLVED"):
        raise OpsError(400, "expected ?group=apps|platform&endpoint=<name>&state=TRIGGERED|RESOLVED")
    if incidents._manager is None:
        raise OpsError(503, "the alert engine isn't running in this annex-ops (OPS_WATCHERS=0)")
    detail = (await request.body()).decode(errors="replace").strip()[:300]
    incidents._manager.submit(engine.GatusState(subject, state == "TRIGGERED", detail))
    return {"ok": True}


# --- incoming webhooks: the one route without a login (hooks.<domain>) -----------------------------------------
_event_limit = SlidingWindow(config.HOOK_EVENTS_PER_MINUTE)  # verified events per hook per minute
_reject_log_limit = SlidingWindow(30)                         # rejected requests we write down, per hook per minute


def log_hook(msg: str) -> None:
    print(f"hooks: {msg}", file=sys.stderr, flush=True)  # never a body, a secret or a signature


@app.head("/h/{app_name}/{hook_name}")
async def hook_declared(app_name: str, hook_name: str):
    """Trello checks the callback URL with HEAD when a webhook is registered: 200 for a declared hook."""
    return Response(status_code=200 if hooks_config.hook(app_name, hook_name) else 404)


@app.post("/h/{app_name}/{hook_name}")
async def hook_receive(app_name: str, hook_name: str, request: Request):
    """A provider's call: check the signature on the raw body, store the event, answer at once. The delivery worker
    forwards it to the app (and retries). Answers are terse: the sender gets no hints."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.HOOK_MAX_BODY:
        return error(413, "too large")
    body = await request.body()  # the raw bytes: every provider signs exactly these
    if len(body) > config.HOOK_MAX_BODY:
        return error(413, "too large")
    hook = hooks_config.hook(app_name, hook_name)
    if hook is None:
        return error(404, "not found")
    key = f"{app_name}/{hook_name}"
    try:
        hooks_verify.verify(hook["verify"], hook["secret"], request.headers, body,
                            hooks_config.callback_url(app_name, hook_name), time.time())
    except hooks_verify.VerifyError as e:
        log_hook(f"rejected {key}: {e}")
        if _reject_log_limit.allow(key, time.time()):  # a flood of junk must not fill the table
            await run_in_threadpool(hooks_store.insert_rejected, app_name, hook_name, str(e))
        return error(401, "signature check failed")
    # only verified events count against the limit, so junk can't use up a hook's quota
    if not _event_limit.allow(key, time.time()):
        return error(429, "slow down")
    if hook["verify"] == "slack":
        challenge = hooks_verify.parse_slack_challenge(body)
        if challenge is not None:
            return PlainTextResponse(challenge)
    event_id = await run_in_threadpool(
        hooks_store.insert_pending, app_name, hook_name, (request.headers.get("content-type") or "")[:200] or None,
        hooks_verify.forwardable(request.headers), body)
    return {"ok": True, "id": event_id}


@app.post("/v1/email")
async def post_email(request: Request):
    grant = await run_in_threadpool(auth.authenticate, request.headers.get("authorization"))
    raw = await json_body(request, grant.app, "email")
    return await run_in_threadpool(email.send_email_request, grant.app, raw, granted=grant.email)


@app.post("/v1/slack")
async def post_slack(request: Request):
    grant = await run_in_threadpool(auth.authenticate, request.headers.get("authorization"))
    raw = await json_body(request, grant.app, "slack")
    return await run_in_threadpool(slack.post_slack_request, grant.app, raw, granted=grant.slack_channels)
