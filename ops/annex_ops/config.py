"""All settings, from the environment, in one place."""
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
