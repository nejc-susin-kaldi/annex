"""Helpers for the Annex app contract: identity, the data gateway, email, Slack and incoming webhooks.

Keep this file as is; build your app in main.py.
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
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
