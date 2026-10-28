"""The webhook receiver: the one route without a login (hooks.<domain>), in its own container.

It runs from the annex-ops image with only what receiving needs: the hooks files (signing secrets) mounted
read-only, and a database role that may only insert events (annex_hooks). It holds no SMTP access, no Slack
token, no user directory and no Docker access, and runs no background threads. The delivery worker, retries,
alerts and `annex hooks` stay in annex-ops, which reads the stored events (D53, D55).
"""
import sys
import time
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, hooks_config, hooks_store, hooks_verify
from .errors import OpsError
from .ratelimit import SlidingWindow

app = FastAPI(title="annex-hooks", docs_url=None, redoc_url=None, openapi_url=None)

_event_limit = SlidingWindow(config.HOOK_EVENTS_PER_MINUTE)  # verified events per hook per minute
_reject_log_limit = SlidingWindow(30)                         # rejected requests we write down, per hook per minute


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def log_hook(msg: str) -> None:
    print(f"hooks: {msg}", file=sys.stderr, flush=True)  # never a body, a secret or a signature


@app.middleware("http")
async def only_hooks(request: Request, call_next):
    """Second layer behind Traefik's router (Host + PathPrefix /h/): nothing but /h/ is served from outside.
    /healthz answers only the container's own healthcheck (Host 127.0.0.1)."""
    host = request.headers.get("host", "").split(":")[0].lower()
    path = request.url.path
    if not (path.startswith("/h/") or (path == "/healthz" and host in ("127.0.0.1", "localhost"))):
        return error(404, "not found")
    return await call_next(request)


# every answer on this host is terse: the sender gets no hints; the reasons go to this container's log
@app.exception_handler(OpsError)
async def _ops_error(_: Request, e: OpsError):
    return error(e.status, "unavailable" if e.status >= 500 else "request refused")


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException):
    return error(e.status_code, "not found" if e.status_code == 404 else "request refused")


@app.exception_handler(Exception)
async def _unexpected(_: Request, e: Exception):
    traceback.print_exception(e, file=sys.stderr)
    return error(500, "error")


@app.get("/healthz")
def healthz():
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except OpsError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=503)
    return {"ok": True}


@app.head("/h/{app_name}/{hook_name}")
async def hook_declared(app_name: str, hook_name: str):
    """Trello checks the callback URL with HEAD when a webhook is registered: 200 for a declared hook."""
    return Response(status_code=200 if hooks_config.hook(app_name, hook_name) else 404)


@app.post("/h/{app_name}/{hook_name}")
async def hook_receive(app_name: str, hook_name: str, request: Request):
    """A provider's call: check the signature on the raw body, store the event, answer at once. annex-ops's
    delivery worker forwards it to the app (and retries)."""
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
    log_hook(f"stored {key} as event {event_id}")
    return {"ok": True, "id": event_id}
