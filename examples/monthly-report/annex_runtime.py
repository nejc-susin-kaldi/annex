"""Helpers for the Annex app contract: identity, logging, the data gateway, email, Slack and incoming webhooks.

Keep this file as is; build your app in main.py.
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class User:
    login: str
    name: str
    email: str
    groups: list[str]


def current_user(headers) -> User:
    """The signed-in user, from the headers Annex's front door sets (and strips from clients)."""
    login = headers.get("remote-user", "")
    return User(
        login=login,
        name=headers.get("remote-name") or login,
        email=headers.get("remote-email", ""),
        groups=[g for g in headers.get("remote-groups", "").split(",") if g],
    )


# --- Logging: one JSON object per line on stdout (the Logging section of `annex contract`) ----------------------
# Required: ts, level (debug|info|warn|error), msg. Inside request_context() every line also gets request_id and
# user. LOG_LEVEL (injected, default info) sets the threshold. Never log secrets, tokens, passwords, request
# bodies or full product records: IDs and counts are fine.
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
           "error": logging.ERROR}
_NAMES = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warn", logging.ERROR: "error",
          logging.CRITICAL: "error"}
_RESERVED = ("ts", "level", "msg", "request_id", "user")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("annex_request_id", default="")
_log_user: contextvars.ContextVar[str] = contextvars.ContextVar("annex_log_user", default="")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
               "level": _NAMES.get(record.levelno, "info"), "msg": record.getMessage()}
        if _request_id.get():
            out["request_id"] = _request_id.get()
        if _log_user.get():
            out["user"] = _log_user.get()
        for key, value in (getattr(record, "annex_fields", None) or {}).items():
            out[key + "_" if key in _RESERVED else key] = value  # a field can't overwrite ts, level, msg, ...
        if record.exc_info and record.exc_info[0]:
            out["exc"] = f"{record.exc_info[0].__name__}: {record.exc_info[1]}"
            out["stack"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def _configure_logging() -> None:
    """Everything that uses Python's logging (uvicorn, libraries) comes out in the same format, on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_LEVELS.get(os.environ.get("LOG_LEVEL", "info").strip().lower(), logging.INFO))


class _Log:
    """log.info("report written", rows=12, ms=340). Extra keyword arguments become fields of the line."""

    def __init__(self, name: str = "annex"):
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, msg: str, fields: dict, exc: bool = False) -> None:
        self._logger.log(level, msg, extra={"annex_fields": fields}, exc_info=exc or None, stacklevel=3)

    def debug(self, msg: str, **fields) -> None:
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields) -> None:
        self._emit(logging.INFO, msg, fields)

    def warn(self, msg: str, **fields) -> None:
        self._emit(logging.WARNING, msg, fields)

    warning = warn

    def error(self, msg: str, **fields) -> None:
        self._emit(logging.ERROR, msg, fields)

    def exception(self, msg: str, **fields) -> None:
        """An error line with the exception being handled (its type, message and stack)."""
        self._emit(logging.ERROR, msg, fields, exc=True)


_configure_logging()
log = _Log()


def new_request_id() -> str:
    """16 random hex characters."""
    return secrets.token_hex(8)


@contextmanager
def request_context(headers):
    """Set request_id and user on every log line inside the block, and yield the request id. An incoming
    X-Request-Id is reused if it looks sane (8-64 letters, digits, '.', '_' or '-'), else a new one is made;
    send it back as the X-Request-Id response header so one id links the access log, the app's lines and an error.
    `headers` is a mapping with case-insensitive keys (Starlette's request.headers, http.server's self.headers)."""
    rid = headers.get("x-request-id") or headers.get("X-Request-Id") or ""
    rid = rid if _REQUEST_ID_RE.match(rid) else new_request_id()
    t1 = _request_id.set(rid)
    t2 = _log_user.set(headers.get("remote-user") or headers.get("Remote-User") or "")
    try:
        yield rid
    finally:
        _request_id.reset(t1)
        _log_user.reset(t2)


# --- Errors in the browser (the Logging section of `annex contract`) ------------------------------------------------
# A page that includes <script src="/annex-errors.js"> reports uncaught errors to POST /api/_errors on its own app,
# which logs one error line (source=browser, with the user and request id) so they are grouped and emailed like
# server errors. Wire it up with two routes: serve browser_errors_js() at /annex-errors.js, and call
# report_browser_error(request.headers, body) in POST /api/_errors and answer with the status it returns.
BROWSER_ERRORS_PER_MINUTE = 10
_browser_hits: dict[str, list[float]] = {}


def browser_errors_js() -> str:
    return Path(__file__).with_name("annex-errors.js").read_text()


def report_browser_error(headers, body: bytes) -> int:
    """Log one error line for a browser error report and return the HTTP status to answer with: 204 logged, 400 not a
    report, 413 too big, 429 too many from this user (10 a minute)."""
    if len(body) > 8192:
        return 413
    try:
        data = json.loads(body)
    except ValueError:
        return 400
    if not isinstance(data, dict):
        return 400
    key = headers.get("remote-user") or headers.get("Remote-User") or "anonymous"
    now = time.monotonic()
    hits = [t for t in _browser_hits.get(key, []) if now - t < 60]
    if len(hits) >= BROWSER_ERRORS_PER_MINUTE:
        _browser_hits[key] = hits
        return 429
    _browser_hits[key] = hits + [now]
    if len(_browser_hits) > 1000:
        _browser_hits.clear()
    log.error(f"browser error: {str(data.get('msg') or 'unknown error')[:500]}", source="browser",
              url=str(data.get("url") or "")[:200], stack=str(data.get("stack") or "")[:1500])
    return 204


class GatewayError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"gateway returned {status}: {message}")


def gateway(view: str, **params: str | int) -> list[dict]:
    """Read a gateway view. Filters use PostgREST syntax, e.g.
    gateway("client_summary", client_name="ilike.*novak*", order="aum_eur.desc", limit=20)
    """
    base = os.environ.get("ANNEX_GATEWAY_URL")
    if not base:
        raise GatewayError(0, "no gateway access: add the view under data.views in annex.yaml")
    url = f"{base.rstrip('/')}/{view}" + (f"?{urlencode(params)}" if params else "")
    req = Request(url, headers={"Authorization": f"Bearer {os.environ['ANNEX_GATEWAY_TOKEN']}",
                                "Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.load(r)
    except HTTPError as e:
        raise GatewayError(e.code, e.read().decode(errors="replace")[:300]) from None
    except URLError as e:
        raise GatewayError(0, str(e.reason)) from None


class NotifyError(Exception):
    """An email or Slack call failed. .status is the HTTP status (0: annex-ops wasn't reachable or the app
    didn't declare notify:). The message is the server's own and says what to fix."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _ops(path: str, payload: dict) -> dict:
    base = os.environ.get("ANNEX_OPS_URL")
    if not base:
        raise NotifyError(0, "add notify: {email: true} (or slack: [...]) to annex.yaml and deploy again")
    req = Request(f"{base.rstrip('/')}{path}", data=json.dumps(payload).encode(), method="POST",
                  headers={"Authorization": f"Bearer {os.environ['ANNEX_OPS_TOKEN']}",
                           "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=20) as r:
            return json.load(r)
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            message = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            message = body[:300]
        raise NotifyError(e.code, message) from None
    except URLError as e:
        raise NotifyError(0, f"can't reach annex-ops ({e.reason}). Try again in a minute") from None


def send_email(to, subject: str, *, text: str | None = None, html: str | None = None,
               template: str | None = None, data: dict | None = None) -> dict:
    """Send one email through Annex. `to` is a login ("cm"), a company address or a list of them; give exactly
    one of text, html or template ("notice" with data={"title", "body", "link", "link_text"}). No attachments:
    link to a file in your app instead. Returns {"id", "to": [resolved addresses]}.
    Needs notify: {email: true} in annex.yaml. Raises NotifyError."""
    payload = {"to": to, "subject": subject, "text": text, "html": html, "template": template, "data": data}
    return _ops("/v1/email", {k: v for k, v in payload.items() if v is not None})


def post_slack(channel: str, text: str, blocks: list | None = None) -> dict:
    """Post to a Slack channel the app declared under notify.slack in annex.yaml, e.g. "#finance-reports".
    Returns {"id", "channel", "ts"}. Raises NotifyError."""
    return _ops("/v1/slack", {"channel": channel, "text": text, **({"blocks": blocks} if blocks else {})})


class HookError(Exception):
    """A webhook delivery from Annex isn't authentic. .status is the HTTP status to answer with (401)."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


@dataclass
class HookEvent:
    hook: str      # the webhook's name in annex.yaml
    event_id: str  # the same for retries and replays: dedupe on it, deliveries are at least once
    attempt: int   # 1 for the first delivery


def hook_event(headers) -> HookEvent:
    """Check that a request really comes from Annex (annex-ops, after it verified the provider's signature) and
    return what it says. Call it first in every webhook endpoint (declared under webhooks: in annex.yaml); a
    HookError (.status 401) means answer 401 and do nothing. Answer 2xx quickly, and skip an event_id you have seen."""
    expected = os.environ.get("ANNEX_HOOK_TOKEN", "")
    got = headers.get("x-annex-hook-token") or headers.get("X-Annex-Hook-Token") or ""
    if not expected or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HookError(401, "missing or wrong X-Annex-Hook-Token: this endpoint takes webhooks forwarded by Annex only")
    try:
        attempt = int(headers.get("x-annex-hook-attempt") or headers.get("X-Annex-Hook-Attempt") or 1)
    except ValueError:
        attempt = 1
    return HookEvent(hook=headers.get("x-annex-hook") or headers.get("X-Annex-Hook") or "",
                     event_id=headers.get("x-annex-hook-event") or headers.get("X-Annex-Hook-Event") or "", attempt=attempt)
