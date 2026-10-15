import base64
import hashlib
import hmac
import json

import pytest

from annex_ops import hooks_verify as hv

NOW = 1_800_000_000


def sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def fails(kind, secret, headers, body, url="https://hooks.annex.example/h/app/x", now=NOW) -> str:
    with pytest.raises(hv.VerifyError) as e:
        hv.verify(kind, secret, headers, body, url, now)
    return str(e.value)


# --- GitHub: the published vector --------------------------------------------------------------------
GH_SECRET, GH_BODY = "It's a Secret to Everybody", b"Hello, World!"
GH_SIG = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def test_github_published_vector():
    hv.verify("github", GH_SECRET, {"X-Hub-Signature-256": GH_SIG}, GH_BODY, "", NOW)
    hv.verify("github", GH_SECRET, {"x-hub-signature-256": GH_SIG}, GH_BODY, "", NOW)  # header names are case-insensitive


def test_github_rejects_wrong_secret_body_prefix_and_missing_header():
    assert "mismatch" in fails("github", "other", {"X-Hub-Signature-256": GH_SIG}, GH_BODY)
    assert "mismatch" in fails("github", GH_SECRET, {"X-Hub-Signature-256": GH_SIG}, GH_BODY + b" ")
    assert "sha256=" in fails("github", GH_SECRET, {"X-Hub-Signature-256": GH_SIG[7:]}, GH_BODY)
    assert "missing header" in fails("github", GH_SECRET, {}, GH_BODY)
    assert "mismatch" in fails("github", GH_SECRET, {"X-Hub-Signature-256": "sha256="}, GH_BODY)


# --- Slack: the published worked example ---------------------------------------------------------------------
SLACK_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
SLACK_TS = "1531420618"
SLACK_BODY = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
              b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
              b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
              b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
SLACK_SIG = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def test_slack_published_example():
    h = {"X-Slack-Signature": SLACK_SIG, "X-Slack-Request-Timestamp": SLACK_TS}
    hv.verify("slack", SLACK_SECRET, h, SLACK_BODY, "", int(SLACK_TS) + 10)


def test_slack_stale_future_and_tampered():
    h = {"X-Slack-Signature": SLACK_SIG, "X-Slack-Request-Timestamp": SLACK_TS}
    assert "off" in fails("slack", SLACK_SECRET, h, SLACK_BODY, now=int(SLACK_TS) + 301)
    assert "off" in fails("slack", SLACK_SECRET, h, SLACK_BODY, now=int(SLACK_TS) - 301)
    hv.verify("slack", SLACK_SECRET, h, SLACK_BODY, "", int(SLACK_TS) + 300)  # exactly at the limit
    assert "mismatch" in fails("slack", SLACK_SECRET, h, SLACK_BODY + b"x", now=int(SLACK_TS))
    assert "missing" in fails("slack", SLACK_SECRET, {"X-Slack-Signature": SLACK_SIG}, SLACK_BODY, now=int(SLACK_TS))
    assert "not a number" in fails("slack", SLACK_SECRET, {**h, "X-Slack-Request-Timestamp": "yesterday"}, SLACK_BODY)


def test_slack_challenge_parsing():
    assert hv.parse_slack_challenge(b'{"type": "url_verification", "challenge": "3eZbrw1aBm2", "token": "x"}') == "3eZbrw1aBm2"
    assert hv.parse_slack_challenge(b'{"type": "event_callback"}') is None
    assert hv.parse_slack_challenge(b"token=a&b=c") is None
    assert hv.parse_slack_challenge(b'{"type": "url_verification", "challenge": 5}') is None
    assert hv.parse_slack_challenge(b"[1]") is None


# --- Stripe: a vector computed independently (openssl) -----------------------------------------------------------
STRIPE_SECRET, STRIPE_T, STRIPE_BODY = "whsec_test_secret", "1492774577", b'{"id":"evt_test"}'
STRIPE_V1 = "22f7d74836ddad3284a2b80853dd2fe731655c1bfddcff3c3666a500cf3abd80"


def test_stripe_vector_from_openssl():
    hv.verify("stripe", STRIPE_SECRET, {"Stripe-Signature": f"t={STRIPE_T},v1={STRIPE_V1}"}, STRIPE_BODY, "", int(STRIPE_T) + 5)


def test_stripe_several_signatures_and_ignored_schemes():
    header = f"t={STRIPE_T},v1={'0' * 64},v0={'1' * 64},v1={STRIPE_V1}"  # a rolled secret sends two v1
    hv.verify("stripe", STRIPE_SECRET, {"Stripe-Signature": header}, STRIPE_BODY, "", int(STRIPE_T))
    assert "mismatch" in fails("stripe", STRIPE_SECRET, {"Stripe-Signature": f"t={STRIPE_T},v0={STRIPE_V1}"}, STRIPE_BODY, now=int(STRIPE_T)) \
        or "no valid" in fails("stripe", STRIPE_SECRET, {"Stripe-Signature": f"t={STRIPE_T},v0={STRIPE_V1}"}, STRIPE_BODY, now=int(STRIPE_T))


def test_stripe_stale_timestamp_wrong_secret_and_malformed():
    good = {"Stripe-Signature": f"t={STRIPE_T},v1={STRIPE_V1}"}
    assert "off" in fails("stripe", STRIPE_SECRET, good, STRIPE_BODY, now=int(STRIPE_T) + 301)
    assert "mismatch" in fails("stripe", "whsec_other", good, STRIPE_BODY, now=int(STRIPE_T))
    assert "mismatch" in fails("stripe", STRIPE_SECRET, good, STRIPE_BODY + b" ", now=int(STRIPE_T))
    for bad in ("", "garbage", f"v1={STRIPE_V1}", f"t=abc,v1={STRIPE_V1}", f"t={STRIPE_T}"):
        assert fails("stripe", STRIPE_SECRET, {"Stripe-Signature": bad}, STRIPE_BODY, now=int(STRIPE_T))
    assert "missing" in fails("stripe", STRIPE_SECRET, {}, STRIPE_BODY)


# --- Trello: a vector computed independently (openssl), signing body + callback URL ----------------------------------
TRELLO_SECRET, TRELLO_BODY = "trello_test_secret", b'{"action":{"type":"updateCard"}}'
TRELLO_URL = "https://hooks.annex.home.arpa:8443/h/hook-probe/trello"
TRELLO_SIG = "g69j3t+sHNSe9jS8By1vaeQ0UzA="


def test_trello_vector_from_openssl():
    hv.verify("trello", TRELLO_SECRET, {"X-Trello-Webhook": TRELLO_SIG}, TRELLO_BODY, TRELLO_URL, NOW)


def test_trello_signs_the_exact_callback_url():
    h = {"X-Trello-Webhook": TRELLO_SIG}
    assert "mismatch" in fails("trello", TRELLO_SECRET, h, TRELLO_BODY, TRELLO_URL.replace(":8443", ""))
    assert "mismatch" in fails("trello", TRELLO_SECRET, h, TRELLO_BODY, TRELLO_URL + "/")
    assert "mismatch" in fails("trello", TRELLO_SECRET, h, TRELLO_BODY + b" ", TRELLO_URL)
    assert "mismatch" in fails("trello", "other", h, TRELLO_BODY, TRELLO_URL)
    assert "missing" in fails("trello", TRELLO_SECRET, {}, TRELLO_BODY, TRELLO_URL)


def test_trello_matches_a_second_implementation():
    digest = hmac.new(b"k", b"body" + b"https://x/h/a/b", hashlib.sha1).digest()
    hv.verify("trello", "k", {"X-Trello-Webhook": base64.b64encode(digest).decode()}, b"body", "https://x/h/a/b", NOW)


# --- shared secret and the rest ----------------------------------------------------------------------------------------
def test_shared_secret():
    hv.verify("shared-secret", "s3cret", {"X-Annex-Hook-Secret": "s3cret"}, b"", "", NOW)
    assert "mismatch" in fails("shared-secret", "s3cret", {"X-Annex-Hook-Secret": "s3cret "}, b"")
    assert "mismatch" in fails("shared-secret", "s3cret", {"X-Annex-Hook-Secret": "S3CRET"}, b"")
    assert "missing" in fails("shared-secret", "s3cret", {}, b"")


def test_an_empty_secret_never_verifies():
    assert "no signing secret" in fails("shared-secret", "", {"X-Annex-Hook-Secret": ""}, b"")
    assert "no signing secret" in fails("github", "", {"X-Hub-Signature-256": "sha256=" + sha256_hex("", b"x")}, b"x")


def test_an_unknown_provider_is_a_key_error_not_a_pass():
    with pytest.raises(KeyError):
        hv.verify("nope", "s", {}, b"", "", NOW)


def test_all_providers_are_covered_by_the_manifest_list():
    assert set(hv.CHECKS) == {"trello", "stripe", "github", "slack", "shared-secret"}


# --- what gets stored and forwarded -----------------------------------------------------------------------------------------
def test_only_allowlisted_provider_headers_are_kept_and_signatures_never():
    kept = hv.forwardable({"Content-Type": "application/json", "User-Agent": "GitHub-Hookshot/abc", "X-GitHub-Event": "push",
                           "X-Hub-Signature-256": GH_SIG, "Stripe-Signature": "t=1", "X-Slack-Signature": "v0=x",
                           "X-Trello-Webhook": "x", "X-Annex-Hook-Secret": "s3cret", "Authorization": "Bearer x",
                           "Remote-User": "kaldi-admin", "Cookie": "a=b", "X-Slack-Retry-Num": "1"})
    assert kept == {"content-type": "application/json", "user-agent": "GitHub-Hookshot/abc", "x-github-event": "push",
                    "x-slack-retry-num": "1"}


def test_header_values_with_control_characters_are_dropped():
    assert hv.forwardable({"User-Agent": "bad\r\nInjected: 1", "Content-Type": "text/plain"}) == {"content-type": "text/plain"}
    assert hv.forwardable({"User-Agent": "x" * 600}) == {}
