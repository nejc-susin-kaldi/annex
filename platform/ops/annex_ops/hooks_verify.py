"""Provider signature checks for incoming webhooks. Pure: no I/O, no clock (`now` is passed in).

Every provider signs the RAW body bytes, so callers must pass the bytes exactly as received. A check returns None
when the signature is good and raises VerifyError(reason) when it isn't; the reason goes to the log and
`annex hooks`, never to the sender (who only hears "signature check failed"). Everything is compared with
hmac.compare_digest. Formats confirmed against each provider's documentation on 15 Oct 2026.
"""
import base64
import hashlib
import hmac
import json
import re

TOLERANCE_S = 300  # Stripe and Slack: signed timestamps older (or newer) than this are rejected


class VerifyError(Exception):
    """The signature is missing or wrong. The message is for the log, not for the sender."""


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _need(h: dict[str, str], name: str) -> str:
    value = h.get(name.lower())
    if not value:
        raise VerifyError(f"missing header {name}")
    return value


def _hmac_hex(secret: str, message: bytes, algo=hashlib.sha256) -> str:
    return hmac.new(secret.encode(), message, algo).hexdigest()


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_github(secret, headers, body, callback_url, now) -> None:
    """X-Hub-Signature-256: sha256=<hex of HMAC-SHA256(secret, body)>"""
    sig = _need(_lower(headers), "X-Hub-Signature-256")
    if not sig.startswith("sha256="):
        raise VerifyError("X-Hub-Signature-256 does not start with sha256=")
    if not _equal(sig[7:], _hmac_hex(secret, body)):
        raise VerifyError("signature mismatch")


def check_stripe(secret, headers, body, callback_url, now) -> None:
    """Stripe-Signature: t=<unix>,v1=<hex>[,v1=...]. HMAC-SHA256(secret, "<t>." + body) must equal any v1
    (several while a secret is being rolled); other schemes (v0, the test scheme) are ignored."""
    header = _need(_lower(headers), "Stripe-Signature")
    t, v1 = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            t = value
        elif key == "v1":
            v1.append(value)
    if not t or not t.isdigit() or not v1:
        raise VerifyError("Stripe-Signature has no valid t= and v1=")
    expected = _hmac_hex(secret, t.encode() + b"." + body)
    if not any(_equal(v, expected) for v in v1):
        raise VerifyError("signature mismatch")
    if abs(now - int(t)) > TOLERANCE_S:
        raise VerifyError(f"timestamp is {abs(now - int(t)):.0f} s off (limit {TOLERANCE_S} s)")


def check_slack(secret, headers, body, callback_url, now) -> None:
    """X-Slack-Signature: v0=<hex of HMAC-SHA256(secret, "v0:<X-Slack-Request-Timestamp>:" + body)>"""
    h = _lower(headers)
    sig, ts = _need(h, "X-Slack-Signature"), _need(h, "X-Slack-Request-Timestamp")
    if not ts.isdigit():
        raise VerifyError("X-Slack-Request-Timestamp is not a number")
    expected = "v0=" + _hmac_hex(secret, b"v0:" + ts.encode() + b":" + body)
    if not _equal(sig, expected):
        raise VerifyError("signature mismatch")
    if abs(now - int(ts)) > TOLERANCE_S:
        raise VerifyError(f"timestamp is {abs(now - int(ts)):.0f} s off (limit {TOLERANCE_S} s)")


def check_trello(secret, headers, body, callback_url, now) -> None:
    """X-Trello-Webhook: base64(HMAC-SHA1(secret, body + callback URL)). The callback URL is the one the webhook
    was registered with, exactly (so it includes the port while testing through the tunnel)."""
    sig = _need(_lower(headers), "X-Trello-Webhook")
    digest = hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
    if not _equal(sig, base64.b64encode(digest).decode()):
        raise VerifyError("signature mismatch")


def check_shared_secret(secret, headers, body, callback_url, now) -> None:
    """X-Annex-Hook-Secret: <the secret itself>"""
    if not _equal(_need(_lower(headers), "X-Annex-Hook-Secret"), secret):
        raise VerifyError("secret mismatch")


CHECKS = {"github": check_github, "stripe": check_stripe, "slack": check_slack, "trello": check_trello,
          "shared-secret": check_shared_secret}


def verify(kind: str, secret: str, headers, body: bytes, callback_url: str, now: float) -> None:
    """Check one request. Raises VerifyError (the reason is for the log) or KeyError for an unknown `kind`."""
    if not secret:
        raise VerifyError("no signing secret is configured for this hook")
    CHECKS[kind](secret, headers, body, callback_url, now)


def parse_slack_challenge(body: bytes) -> str | None:
    """The `challenge` of a Slack url_verification request (check the signature first), else None."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("type") == "url_verification" and isinstance(data.get("challenge"), str):
        return data["challenge"]
    return None


# --- the stored headers --------------------------------------------------------------------------
# Signature headers are not stored or forwarded: Annex has checked them. Only these provider headers are.
FORWARD_HEADERS = ("content-type", "user-agent", "x-github-event", "x-github-delivery", "x-slack-retry-num")
HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]{0,500}$")


def forwardable(headers) -> dict[str, str]:
    """The allowlisted provider headers of a request (lower-case names, printable ASCII values only)."""
    h = _lower(headers)
    return {k: h[k] for k in FORWARD_HEADERS if k in h and HEADER_VALUE_RE.match(h[k])}
