"""All settings, from the environment, in one place."""
import json
import os


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


DATABASE_URL = os.environ.get("OPS_DATABASE_URL", "")
USERS_FILE = os.environ.get("OPS_USERS_FILE", "/annex/directory/users.json")

EMAIL_DOMAINS = [d.lower() for d in _list("ANNEX_EMAIL_DOMAINS", "annex.example")]
EMAIL_ALLOW = [a.lower() for a in _list("ANNEX_EMAIL_ALLOW")]
EMAIL_FROM = os.environ.get("ANNEX_EMAIL_FROM", "noreply@annex.example")
SMTP_HOST = os.environ.get("OPS_SMTP_HOST", "mailpit")
SMTP_PORT = _int("OPS_SMTP_PORT", 1025)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_API = os.environ.get("OPS_SLACK_API", "https://slack.com/api").rstrip("/")

EMAIL_PER_HOUR = _int("OPS_EMAIL_PER_HOUR", 60)
SLACK_PER_HOUR = _int("OPS_SLACK_PER_HOUR", 60)
MAX_BODY_BYTES = 256 * 1024


def slack_connected() -> bool:
    return bool(SLACK_BOT_TOKEN)


# --- alerts and monitoring (M1.3) ---------------------------------------------
WATCHERS = os.environ.get("OPS_WATCHERS", "1") != "0"  # unit tests turn the background threads off
DOCKER_API = os.environ.get("OPS_DOCKER_API", "socket-ro:2375")  # the read-only socket proxy
GATUS_URL = os.environ.get("OPS_GATUS_URL", "http://gatus:8080").rstrip("/")
INTERNAL_TOKEN = os.environ.get("OPS_INTERNAL_TOKEN", "")  # Gatus's webhook to /internal/gatus
TUNING_FILE = os.environ.get("OPS_TUNING_FILE", "/annex/state/tuning.json")
ALERT_SLACK_CHANNEL = os.environ.get("ANNEX_ALERT_SLACK_CHANNEL", "#annex-alerts")
STATUS_URL = os.environ.get("ANNEX_URL_STATUS", "")
HOST_PATTERN = os.environ.get("ANNEX_HOST_PATTERN", "")
PORT_SUFFIX = os.environ.get("ANNEX_PORT_SUFFIX", "")

_tuning_cache: dict = {"mtime": None, "values": {}}


def tuning():
    """The alert thresholds: engine defaults, overridden by OPS_<NAME> environment variables (e.g.
    OPS_STOPPED_GRACE_S) and, at runtime, by tuning.json (re-read when its mtime changes). Production never
    has the file; scripts/test-alerts.sh writes a short one."""
    from dataclasses import fields

    from .engine import Tuning

    try:
        mtime = os.stat(TUNING_FILE).st_mtime_ns
    except OSError:
        mtime = None
    if mtime != _tuning_cache["mtime"]:
        values = {}
        if mtime is not None:
            try:
                with open(TUNING_FILE) as f:
                    values = json.load(f)
            except (OSError, ValueError):
                values = {}
        _tuning_cache.update(mtime=mtime, values=values)
    out = Tuning()
    for f in fields(Tuning):
        raw = _tuning_cache["values"].get(f.name, os.environ.get("OPS_" + f.name.upper()))
        if raw is not None:
            try:
                setattr(out, f.name, type(getattr(out, f.name))(raw))
            except (TypeError, ValueError):
                pass
    return out


def app_url(app: str) -> str:
    return f"https://{HOST_PATTERN.replace('{name}', app)}{PORT_SUFFIX}" if HOST_PATTERN else ""
