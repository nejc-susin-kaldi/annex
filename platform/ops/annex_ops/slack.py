"""POST /v1/slack: channel allowlist, then chat.postMessage if a bot token is configured."""
import json
import re
import urllib.error
import urllib.request

from . import config, sends
from .errors import OpsError

FIELDS = ("channel", "text", "blocks")
CHANNEL_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,79}$")
NOT_CONNECTED = ("Slack isn't connected on this Annex yet, so nothing was posted. Send an email instead, "
                 "or ask the platform team to connect Slack")


def parse_channel(raw: object) -> str:
    """The `channel` field, normalised (lower-case). Raises OpsError(400)."""
    if not isinstance(raw, dict):
        raise OpsError(400, 'the body must be a JSON object like {"channel": "#finance-reports", "text": "..."}')
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise OpsError(400, f"unknown field(s): {', '.join(unknown)}. Allowed fields: {', '.join(FIELDS)}")
    channel = raw.get("channel")
    if not isinstance(channel, str) or not CHANNEL_RE.match(channel.strip().lower()):
        raise OpsError(400, f"channel: {channel!r} is not valid. Use the channel name with '#', e.g. \"#finance-reports\"")
    return channel.strip().lower()


def check_channel(channel: str, granted: list[str] | None) -> None:
    """403 unless the channel is one the app declared. `granted=None` skips the check (internal callers)."""
    if granted is None:
        return
    allowed = [c.lower() for c in granted]
    if channel not in allowed:
        only = ", ".join(allowed) or "no channels"
        raise OpsError(403, f"this app may post only to: {only}. Add the channel under notify.slack in "
                            "annex.yaml and deploy again")


def parse_message(raw: dict) -> tuple[str | None, list | None]:
    text, blocks = raw.get("text"), raw.get("blocks")
    if blocks is not None and (not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks)):
        raise OpsError(400, "blocks: must be a list of Slack block objects")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise OpsError(400, "text: must be a non-empty string")
    if text is None:
        why = "text is required whenever blocks is given, as the notification fallback" if blocks else \
              "give text (or blocks together with text)"
        raise OpsError(400, why)
    return text, blocks or None


# Slack answers 200 with {"ok": false, "error": "..."}; these say how to fix the common ones
SLACK_ERRORS = {
    "not_in_channel": "invite the Annex app to {channel} in Slack (/invite @Annex), then try again",
    "channel_not_found": "{channel} doesn't exist or is private and the Annex app isn't in it. Create it or "
                         "invite the Annex app, then try again",
    "is_archived": "{channel} is archived. Unarchive it or declare another channel in annex.yaml",
    "invalid_auth": "the platform's Slack token is not valid. The platform team replaces it (docs/operations.md)",
    "token_revoked": "the platform's Slack token was revoked. The platform team replaces it (docs/operations.md)",
    "missing_scope": "the Slack app needs the chat:write scope. The platform team adds it and reinstalls the app",
    "rate_limited": "Slack is rate limiting this workspace. Try again in a minute",
}


def redirect(channel: str, text: str, active: bool, test_channel: str) -> tuple[str, str, str | None]:
    """(channel, text, note) to post. In test mode the post goes to the test channel, marked, and says where it was
    meant to go; the note goes in the send log. Otherwise nothing changes."""
    if not active:
        return channel, text, None
    return test_channel, f"[TEST] {text}  (test run: this was meant for {channel})", f"test mode: posted to {test_channel}"


def deliver(channel: str, text: str, blocks: list | None) -> str:
    """Post one message. Returns Slack's message timestamp. Raises OpsError(502)."""
    payload = {"channel": channel, "text": text, **({"blocks": blocks} if blocks else {})}
    req = urllib.request.Request(
        f"{config.SLACK_API}/chat.postMessage", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OpsError(502, f"couldn't reach Slack ({type(e).__name__}). Try again in a minute; if it keeps "
                            "failing the platform team checks the VM's internet access") from None
    if not body.get("ok"):
        err = str(body.get("error", "unknown_error"))
        fix = SLACK_ERRORS.get(err, "check the message and the channel").format(channel=channel)
        raise OpsError(502, f"Slack refused the post ({err}): {fix}")
    return str(body.get("ts", ""))


def post_slack_request(app: str, raw: object, *, granted: list[str] | None = None) -> dict:
    """Everything after authentication: channel, shape, rate limit, connected?, post, log.
    `granted` is the app's channels; None skips the channel check (internal callers)."""
    channel = None
    try:
        channel = parse_channel(raw)
        check_channel(channel, granted)
        text, blocks = parse_message(raw)
        sends.check_rate(app, "slack", config.SLACK_PER_HOUR, "Slack posts")
        if not config.slack_connected():
            raise OpsError(503, NOT_CONNECTED)
        target, shown, note = redirect(channel, text, config.test_mode_until() is not None, config.TEST_SLACK_CHANNEL)
        ts = deliver(target, shown, blocks)
    except OpsError as e:
        shown = channel or (raw.get("channel") if isinstance(raw, dict) and isinstance(raw.get("channel"), str) else "")
        sends.log_failure(app, "slack", [str(shown)[:100]] if shown else [], None, e)
        raise
    return {"id": sends.log_send(app, "slack", [channel], note, "sent"), "channel": channel, "ts": ts}


def post_slack(app: str, channel: str, text: str, blocks: list | None = None) -> dict:
    """For code inside annex-ops (M1.3's alerts, app="_platform"): no token or channel grant check,
    but the channel name is validated, rate limits apply and the post is logged."""
    return post_slack_request(app, {"channel": channel, "text": text, **({"blocks": blocks} if blocks else {})})
