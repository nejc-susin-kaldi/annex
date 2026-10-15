"""The annex-ops HTTP API (apps reach it as ANNEX_OPS_URL on the edge network)."""
import json
import sys
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, config, db, email, sends, slack
from .errors import OpsError

app = FastAPI(title="annex-ops", docs_url=None, redoc_url=None, openapi_url=None)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.exception_handler(OpsError)
async def _ops_error(_: Request, e: OpsError):
    return error(e.status, e.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, e: StarletteHTTPException):
    if e.status_code == 404:
        return error(404, f"no such endpoint: {request.method} {request.url.path}. "
                          "Endpoints: GET /v1/me, POST /v1/email, POST /v1/slack")
    if e.status_code == 405:
        return error(405, f"{request.method} isn't allowed on {request.url.path}. Use POST for /v1/email and /v1/slack")
    return error(e.status_code, str(e.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'] if p != 'body')}: {err['msg']}" for err in e.errors())
    return error(400, f"invalid request ({problems}). See the endpoint's JSON in annex contract")


@app.exception_handler(Exception)
async def _unexpected(request: Request, e: Exception):
    traceback.print_exception(e, file=sys.stderr)
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
