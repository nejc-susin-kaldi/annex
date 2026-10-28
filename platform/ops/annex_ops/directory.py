"""The user directory: login -> name, email, groups.

Bootstrap writes users.json (no password hashes) into a directory that is mounted read-only.
It is re-read whenever the file changes, so a new user needs no restart.
"""
import json
import os

from . import config
from .errors import OpsError

_cache: dict = {"mtime": None, "users": {}}


def users(path: str | None = None) -> dict[str, dict]:
    path = path or config.USERS_FILE
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        raise OpsError(503, f"annex-ops can't read the user directory ({path}), so it can't turn logins into "
                            "email addresses. Use full company addresses for now; the platform team runs "
                            "scripts/bootstrap.sh") from None
    if _cache["mtime"] != mtime or _cache.get("path") != path:
        with open(path) as f:
            _cache.update(mtime=mtime, path=path, users=json.load(f))
    return _cache["users"]
