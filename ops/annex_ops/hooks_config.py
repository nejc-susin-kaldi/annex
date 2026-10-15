"""Each app's webhook file (/annex/hooks/<app>.json), written by annex_vm.py provision and mounted read-only.

It holds the signing secrets, so it is only ever read here, never logged, and `describe` (used by ctl and error
messages) leaves the secrets out. Re-read when its mtime changes, like the user directory.
"""
import json
import os
import re

from . import config

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
_cache: dict[str, tuple[int, dict]] = {}


def load(app: str, directory: str | None = None) -> dict | None:
    """{"token", "port", "hooks": {name: {"verify", "secret", "path"}}} for an app, or None if it has no hooks."""
    if not NAME_RE.match(app):
        return None  # never build a path from anything else
    path = os.path.join(directory or config.HOOKS_DIR, f"{app}.json")
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _cache.pop(path, None)
        return None
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        try:
            with open(path) as f:
                _cache[path] = (mtime, json.load(f))
        except (OSError, ValueError):
            return None
    return _cache[path][1]


def hook(app: str, name: str, directory: str | None = None) -> dict | None:
    cfg = load(app, directory)
    return cfg["hooks"].get(name) if cfg else None


def callback_url(app: str, name: str) -> str:
    """The URL a provider was given when the webhook was registered (Trello signs it into every request)."""
    return f"{config.URL_HOOKS}/h/{app}/{name}"
