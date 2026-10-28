from datetime import datetime, timedelta, timezone

from annex_ops import sends
from annex_ops.errors import OpsError

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def ago(minutes: int) -> datetime:
    return NOW - timedelta(minutes=minutes)


def test_under_the_limit_is_allowed():
    assert sends.seconds_until_allowed([], 60, NOW) == 0
    assert sends.seconds_until_allowed([ago(5)] * 59, 60, NOW) == 0


def test_at_the_limit_waits_until_the_oldest_ages_out():
    sent = [ago(50)] + [ago(5)] * 59  # 60 sends; the oldest leaves the window in 10 minutes
    wait = sends.seconds_until_allowed(sent, 60, NOW)
    assert 600 <= wait <= 602


def test_over_the_limit_waits_for_enough_to_age_out():
    sent = [ago(55), ago(50), ago(10), ago(5)]  # limit 3: two must leave; the second one does in 10 minutes
    assert 600 <= sends.seconds_until_allowed(sent, 3, NOW) <= 602


def test_zero_limit_blocks_everything():
    assert sends.seconds_until_allowed([], 0, NOW) == 3600


def test_failure_status_mapping():
    assert sends.failure_status(OpsError(403, "x")) == "rejected"
    assert sends.failure_status(OpsError(400, "x")) == "rejected"
    assert sends.failure_status(OpsError(429, "x")) == "rejected"
    assert sends.failure_status(OpsError(502, "x")) == "failed"
    assert sends.failure_status(OpsError(503, "x")) == "unavailable"
