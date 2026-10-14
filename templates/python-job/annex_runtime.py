"""Helpers for the Annex app contract: identity and the data gateway.

Keep this file as is; build your app in main.py.
"""
from __future__ import annotations

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
