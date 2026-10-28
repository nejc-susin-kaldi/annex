"""All settings, from the environment, in one place."""
import json
import os


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


DATABASE_URL = os.environ.get("OPS_DATABASE_URL", "")
# appdata-db's name on annex-appdata only (compose sets it for ops). The URL's `appdata-db` also resolves on every app
# network ops shares with appdata-db (D88), where Docker may answer with the app network's address; a connection made
# there hangs when the app is removed and its network goes, and fills the pool (D100).
DATABASE_HOST = os.environ.get("OPS_DATABASE_HOST", "").strip()
DB_POOL_MAX = _int("OPS_DB_POOL_MAX", 8)  # connections this process may hold; compose sets 2 for the console and the receiver (D84)
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
FRONTDOOR_URL = os.environ.get("ANNEX_FRONTDOOR_URL", "")  # what the health checker requests to check the front door (D41)
OPS_URL = os.environ.get("OPS_URL", "http://ops:8000")  # annex-ops as the platform network sees it: the health checker's ops check, the console's thread table (D81)
OPS_SELF_URL = "http://127.0.0.1:8000"  # the same, from inside the ops container (ctl)
TUNING_FILE = os.environ.get("OPS_TUNING_FILE", "/annex/state/tuning.json")
ALERT_SLACK_CHANNEL = os.environ.get("ANNEX_ALERT_SLACK_CHANNEL", "#annex-alerts")
STATUS_URL = os.environ.get("ANNEX_URL_STATUS", "")
HOST_PATTERN = os.environ.get("ANNEX_HOST_PATTERN", "")
PORT_SUFFIX = os.environ.get("ANNEX_PORT_SUFFIX", "")

# test mode (D74): while the file holds a time in the future, every Slack post goes to the test channel instead,
# marked [TEST]. Test scripts switch it on for the length of a run (scripts/testlib.sh) and off again at the end.
TEST_MODE_FILE = os.path.join(os.path.dirname(TUNING_FILE), "test-mode")
TEST_SLACK_CHANNEL = os.environ.get("ANNEX_TEST_SLACK_CHANNEL", "#annex-selftest")


def test_mode_until() -> float | None:
    """When test mode ends (epoch seconds), or None if it is off or the time has passed or the file is unreadable."""
    try:
        with open(TEST_MODE_FILE) as f:
            until = float(f.read().strip())
    except (OSError, ValueError):
        return None
    import time

    return until if time.time() < until else None


_tuning_cache: dict = {"mtime": None, "values": {}}


def _settings(cls):
    """A settings dataclass: its defaults, overridden by OPS_<NAME> environment variables (e.g.
    OPS_STOPPED_GRACE_S) and, at runtime, by tuning.json (re-read when its mtime changes). Production never
    has the file; the test scripts write a short one."""
    from dataclasses import fields

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
    out = cls()
    for f in fields(cls):
        raw = _tuning_cache["values"].get(f.name, os.environ.get("OPS_" + f.name.upper()))
        if raw is not None:
            try:
                setattr(out, f.name, type(getattr(out, f.name))(raw))
            except (TypeError, ValueError):
                pass
    return out


def tuning():
    """The alert thresholds (engine.Tuning)."""
    from .engine import Tuning

    return _settings(Tuning)


def usage_settings():
    """Retention, signal thresholds, rollup interval and digest schedule (usage.UsageSettings)."""
    from .usage import UsageSettings

    return _settings(UsageSettings)


def log_settings():
    """Retention, size limit and rate cap of the stored logs (logship.LogSettings)."""
    from .logship import LogSettings

    return _settings(LogSettings)


def audit_settings():
    """Audit trail: scan interval, gateway bucket, retention (audit.AuditSettings)."""
    from .audit import AuditSettings

    return _settings(AuditSettings)


def error_settings():
    """Error groups: scan interval, quiet period, emails per hour (errors_run.ErrorSettings)."""
    from .errors_run import ErrorSettings

    return _settings(ErrorSettings)


# incoming webhooks (M1.5): the one public hostname, and where each app's signing secrets are mounted
HOST_HOOKS = os.environ.get("ANNEX_HOST_HOOKS", "").lower()
URL_HOOKS = os.environ.get("ANNEX_URL_HOOKS", "").rstrip("/")
HOOKS_DIR = os.environ.get("OPS_HOOKS_DIR", "/annex/hooks")
HOOK_MAX_BODY = 1024 * 1024
HOOK_EVENTS_PER_MINUTE = 60

# usage days and weeks are Europe/Ljubljana (the TZ setting's default): the SQL view uses the same zone
USAGE_TZ = "Europe/Ljubljana"
METABASE_URL = os.environ.get("ANNEX_URL_METABASE", "")


def app_url(app: str) -> str:
    return f"https://{HOST_PATTERN.replace('{name}', app)}{PORT_SUFFIX}" if HOST_PATTERN else ""
