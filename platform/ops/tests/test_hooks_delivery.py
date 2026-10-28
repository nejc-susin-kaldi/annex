from annex_ops import hooks_delivery as d
from annex_ops.ratelimit import SlidingWindow


def ev(id, app="a", hook="h", received=0.0, attempts=0, next_at=None):
    return {"id": id, "app": app, "hook": hook, "received_at": received, "attempts": attempts, "next_attempt_at": next_at}


def test_backoff_steps_and_the_last_one_repeats():
    b = d.parse_backoff("10,30,60,300,900,3600")
    assert [d.retry_delay(n, b) for n in (1, 2, 3, 4, 5, 6, 7, 20)] == [10, 30, 60, 300, 900, 3600, 3600, 3600]
    assert d.retry_delay(0, b) == 10


def test_backoff_parsing_falls_back_to_the_defaults():
    assert d.parse_backoff("3, 5,10") == [3.0, 5.0, 10.0]
    for bad in ("", "x", "0,5", "-1", "3,,"):
        assert d.parse_backoff(bad) == [10.0, 30.0, 60.0, 300.0, 900.0, 3600.0] or bad == "3,,"
    assert d.parse_backoff("3,,") == [3.0]


def test_a_new_event_is_due_at_once_and_a_retry_when_its_time_comes():
    assert [e["id"] for e in d.due_events([ev(1)], now=100)] == [1]
    assert d.due_events([ev(1, attempts=1, next_at=130)], now=100) == []
    assert [e["id"] for e in d.due_events([ev(1, attempts=1, next_at=130)], now=130)] == [1]


def test_a_failing_event_holds_back_newer_events_of_the_same_hook_only():
    pending = [ev(1, attempts=2, next_at=500), ev(2), ev(3, hook="other"), ev(4, app="b")]
    assert sorted(e["id"] for e in d.due_events(pending, now=100)) == [3, 4]


def test_only_the_oldest_event_of_a_hook_is_delivered_per_cycle():
    assert [e["id"] for e in d.due_events([ev(1), ev(2), ev(3)], now=100)] == [1]


def test_hooks_fail_for_the_alert_only_after_the_threshold_and_only_if_attempted():
    rows = [ev(1, received=0, attempts=3), ev(2, app="b", received=0, attempts=0), ev(3, app="c", received=500, attempts=1)]
    assert d.failing_hooks(rows, now=901, after_s=900) == {("a", "h"): 0}   # b never tried, c failing for 401 s
    assert d.failing_hooks(rows, now=900, after_s=900) == {}


def test_the_oldest_event_decides_whether_a_hook_is_failing():
    rows = [ev(1, received=0, attempts=0), ev(2, received=0, attempts=5)]   # the oldest is fresh: nothing is stuck
    assert d.failing_hooks(rows, now=2000, after_s=900) == {}


def test_alert_changes():
    assert d.alert_changes({("a", "h")}, {("a", "h"): 1.0, ("b", "x"): 2.0}) == ([("a", "h"), ("b", "x")], [])
    assert d.alert_changes({("a", "h"), ("c", "z")}, {("a", "h"): 1.0}) == ([("a", "h")], [("c", "z")])


def test_delivery_headers():
    event = {"id": 7, "attempts": 2, "replay_of": None, "content_type": "application/json",
             "headers": {"user-agent": "x", "x-github-event": "push"}}
    h = d.delivery_headers(event, "tok", "gh")
    assert h["X-Annex-Hook"] == "gh" and h["X-Annex-Hook-Event"] == "7" and h["X-Annex-Hook-Attempt"] == "3"
    assert h["X-Annex-Hook-Token"] == "tok" and h["content-type"] == "application/json" and h["x-github-event"] == "push"


def test_a_replay_carries_the_original_event_id_and_starts_at_attempt_one():
    h = d.delivery_headers({"id": 99, "attempts": 0, "replay_of": 7, "content_type": None, "headers": {}}, "tok", "gh")
    assert h["X-Annex-Hook-Event"] == "7" and h["X-Annex-Hook-Attempt"] == "1" and "content-type" not in h


def test_rate_limit_window():
    w = SlidingWindow(3, 60)
    assert [w.allow("a", t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert w.allow("b", 3)                    # another key has its own quota
    assert not w.allow("a", 59)               # refused requests aren't counted, but the window hasn't moved
    assert w.allow("a", 60)                   # the first hit has left the window
    assert [w.allow("a", 61), w.allow("a", 62), w.allow("a", 62.5)] == [True, True, False]  # 60, 61, 62 are in the window
