"""The audit trail's pure parts (M2.5): sign-in lines, buckets, gateway aggregates."""
from annex_ops import audit


def test_a_successful_sign_in():
    assert audit.signin_event("Successful 1FA authentication attempt made by user 'sam'") == {"action": "signin", "actor": "sam", "details": {}}
    assert audit.signin_event("Successful 2FA authentication attempt made by user 'kaldi-admin'")["actor"] == "kaldi-admin"


def test_a_failed_sign_in_keeps_a_login_but_never_something_that_may_be_a_password():
    assert audit.signin_event("Unsuccessful 1FA authentication attempt by user 'cm'") == {"action": "signin.failed", "actor": "cm", "details": {}}
    for typed in ("Tr0ub4dor&3", "correct horse battery staple", "P@ssw0rd!", "", "X" * 40, "UPPER"):
        e = audit.signin_event(f"Unsuccessful 1FA authentication attempt by user '{typed}'")
        assert e["action"] == "signin.failed" and e["actor"] is None and (typed == "" or typed not in str(e)) and e["details"] == {"username": "not a login"}


def test_other_authelia_lines_are_not_sign_ins():
    for msg in ("Mark 1FA authentication attempt made by user 'sam'", "Checking the authentication backend for an updated profile",
                "Access to 'https://x/' is forbidden to user 'cm'", ""):
        assert audit.signin_event(msg) is None


def test_buckets():
    assert audit.bucket_start(3600 * 5 + 1234, 3600) == 3600 * 5
    assert audit.bucket_start(3600 * 5, 3600) == 3600 * 5
    assert audit.bucket_start(125, 60) == 120


def test_a_gateway_aggregate_is_keyed_so_a_recount_replaces_it():
    e = audit.gateway_event("monthly-report", "monthly_positions", 1792152000.0, 3600, 42)
    assert (e["actor"], e["app"], e["action"], e["details"]["view"], e["details"]["requests"]) == ("app:monthly-report", "monthly-report", "gateway.reads", "monthly_positions", 42)
    assert e["ts"] == 1792152000.0 + 3600 and e["ref"] == "gateway:monthly-report:monthly_positions:2026-10-16T12:00:00Z:3600"
    assert audit.gateway_event("monthly-report", "monthly_positions", 1792152000.0, 3600, 50)["ref"] == e["ref"]


def test_the_gateway_scan_waits_for_a_bucket_to_close():
    h = 3600
    assert audit.gateway_window(10 * h + 1800, h, 10 * h) is None  # still inside the bucket the cursor ended at
    assert audit.gateway_window(10 * h, h, 10 * h) is None


def test_the_gateway_scan_replays_one_bucket_before_the_cursor():
    h = 3600
    assert audit.gateway_window(11 * h + 5, h, 10 * h) == (9 * h, 11 * h)
    assert audit.gateway_window(15 * h + 5, h, 10 * h) == (9 * h, 15 * h)  # a gap is caught up, a bucket at a time
    assert audit.gateway_window(11 * h + 5, h, 10 * h + 17) == (9 * h, 11 * h)  # an unaligned cursor is aligned first


def test_the_gateway_scan_never_reaches_back_past_the_window():
    h = 3600
    assert audit.gateway_window(100 * h + 5, h, 10 * h, 48 * h) == (52 * h, 100 * h)
    assert audit.gateway_window(100 * h + 5, h, None, 48 * h) == (52 * h, 100 * h)  # no cursor and no earlier aggregate


def test_scanning_twice_in_a_bucket_reads_nothing_the_second_time():
    h, cursor = 3600, None
    seen = []
    for now in (10 * h + 1, 10 * h + 61, 10 * h + 3000, 11 * h + 1, 11 * h + 61):
        w = audit.gateway_window(now, h, cursor)
        if w:
            seen.append(w)
            cursor = w[1]
    assert [w[1] for w in seen] == [10 * h, 11 * h]
