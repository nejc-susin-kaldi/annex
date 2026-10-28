import json
from datetime import datetime, timedelta, timezone

from annex_ops import usage
from annex_ops.usage import UsageSettings

HEALTH = {"client-lookup": "/healthz"}


def line(**kw):
    e = {"DownstreamStatus": 200, "Duration": 11914061, "RequestMethod": "GET", "RequestPath": "/",
         "RouterName": "app-client-lookup@docker", "StartUTC": "2026-10-14T12:41:52.358737293Z", "request_Remote-User": "cm"}
    e.update(kw)
    return json.dumps({k: v for k, v in e.items() if v is not None})


def parse(**kw):
    return usage.parse_access_line(line(**kw), HEALTH)


# --- what counts -----------------------------------------------------------------------
def test_a_signed_in_page_request_counts_with_the_duration_in_ms():
    r = parse()
    assert (r.app, r.login, r.method, r.path, r.status, r.duration_ms) == ("client-lookup", "cm", "GET", "/", 200, 11)
    assert abs(r.ts - 1791981712.358737) < 1e-3


def test_the_query_string_is_never_kept():
    r = parse(RequestPath="/?q=novak&x=1")
    assert r.path == "/"
    assert parse(RequestPath="/search?q=jane%20doe#top").path == "/search"
    assert "?" not in parse(RequestPath="/a?b?c").path


def test_long_paths_are_capped():
    assert len(parse(RequestPath="/" + "x" * 500).path) == 200


def test_anonymous_requests_dont_count():
    assert parse(**{"request_Remote-User": ""}) is None
    assert parse(**{"request_Remote-User": None}) is None
    assert parse(**{"request_Remote-User": "  "}) is None


def test_the_apps_health_path_doesnt_count_but_other_apps_paths_do():
    assert parse(RequestPath="/healthz") is None
    assert parse(RequestPath="/healthz?x=1") is None
    assert parse(RequestPath="/healthz/deeper") is not None
    assert usage.parse_access_line(line(RouterName="app-other@docker", RequestPath="/healthz"), HEALTH) is not None


def test_static_assets_dont_count():
    for path in ("/favicon.ico", "/static/app.JS", "/a/b.css", "/x.map", "/logo.png", "/f.woff2", "/i.svg", "/m.mjs"):
        assert parse(RequestPath=path) is None, path
    assert parse(RequestPath="/report.csv") is not None and parse(RequestPath="/download/x.pdf") is not None


def test_platform_routers_and_traefiks_own_lines_dont_count():
    assert usage.parse_access_line(line(RouterName="home@docker"), HEALTH) is None
    assert usage.parse_access_line(line(RouterName=None), HEALTH) is None
    assert usage.parse_access_line('time="2026-10-14" level=info msg="x"', HEALTH) is None
    assert usage.parse_access_line("{broken", HEALTH) is None


def test_unusable_entries_are_dropped():
    assert parse(DownstreamStatus=None) is None
    assert parse(StartUTC=None) is None
    assert parse(Duration=None).duration_ms == 0


def test_errors_keep_their_status():
    assert parse(DownstreamStatus=500).status == 500


# --- trend and signals ------------------------------------------------------------------
def test_trend_strings():
    assert usage.trend(5, 3) == "↑2" and usage.trend(1, 4) == "↓3" and usage.trend(2, 2) == "=" and usage.trend(0, 0) == "="


def test_graduation_needs_rising_use_reaching_the_minimum():
    assert usage.graduation([3, 4, 4, 6], 5)
    assert usage.graduation([2, 3, 4, 5], 5)             # exactly the minimum
    assert not usage.graduation([3, 4, 4, 4], 5)         # latest under the minimum
    assert not usage.graduation([5, 5, 5, 5], 5)         # flat: latest isn't higher than the first
    assert not usage.graduation([3, 4, 3, 6], 5)         # a week fell
    assert not usage.graduation([None, 4, 5, 6], 5)      # the app didn't exist for the whole period
    assert not usage.graduation([6], 5) and not usage.graduation([], 5)
    assert usage.graduation([4, 6], 5)                   # two weeks, if configured so


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def test_dormant_at_the_edge_of_the_period():
    old = NOW - timedelta(days=90)
    assert usage.dormant(NOW - timedelta(days=30), old, NOW, 30)
    assert not usage.dormant(NOW - timedelta(days=29, hours=23), old, NOW, 30)
    assert usage.dormant(None, old, NOW, 30)             # never used, and old enough


def test_a_new_app_is_not_dormant_until_it_is_old_enough():
    assert not usage.dormant(None, NOW - timedelta(days=29), NOW, 30)
    assert usage.dormant(None, NOW - timedelta(days=30), NOW, 30)
    assert usage.dormant(None, NOW, NOW, 1, min_age_days=0)   # the digest preview's overrides
    assert not usage.dormant(None, NOW - timedelta(hours=12), NOW, 1)  # the default minimum age is the dormant period


def row(**kw):
    base = {"app": "x", "first_seen": NOW - timedelta(days=90), "last_used": NOW - timedelta(days=1),
            "week1": None, "week2": None, "week3": None, "week4": None, "users_7d": 0, "users_prev_7d": 0, "requests_7d": 0}
    base.update(kw)
    return base


def test_signal_for_picks_dormant_first_then_graduation():
    s = UsageSettings()
    assert usage.signal_for(row(last_used=NOW - timedelta(days=40)), NOW, s) == "dormant"
    assert usage.signal_for(row(week1=3, week2=4, week3=4, week4=6), NOW, s) == "graduation"
    assert usage.signal_for(row(week1=3, week2=4, week3=4, week4=4), NOW, s) is None
    assert usage.signal_for(row(week1=1, week2=1, week3=1, week4=1), NOW, s) is None


def test_graduation_weeks_setting_uses_the_latest_weeks():
    two = UsageSettings(graduation_weeks=2)
    assert usage.signal_for(row(week1=9, week2=1, week3=4, week4=6), NOW, two) == "graduation"
    assert usage.signal_for(row(week1=9, week2=1, week3=4, week4=6), NOW, UsageSettings()) is None
    assert usage.signal_for(row(week3=4, week4=6), NOW, UsageSettings(graduation_weeks=9)) is None  # capped at 4: week1 unknown


def test_signal_overrides_for_the_digest_preview():
    r = row(last_used=NOW - timedelta(days=2), first_seen=NOW - timedelta(days=3))
    assert usage.signal_for(r, NOW, UsageSettings(), dormant_days=1, min_age_days=0) == "dormant"


# --- the tile -----------------------------------------------------------------------------
def test_tile_json_has_totals_and_status_and_no_logins():
    t = usage.tile(row(users_7d=3, users_prev_7d=1, requests_7d=41), [], False, None)
    assert t == {"users_7d": 3, "trend": "↑2", "requests_7d": 41, "status": "ok", "last_run": "none yet"}
    assert "login" not in json.dumps(t).lower()


def test_tile_status_words():
    r = row()
    assert usage.tile(r, ["stopped"], False, None)["status"] == "down"
    assert usage.tile(r, ["down", "stopped"], False, None)["status"] == "down"
    assert usage.tile(r, ["crash-loop"], False, None)["status"] == "crash-looping"
    assert usage.tile(r, ["errors"], False, None)["status"] == "errors"
    assert usage.tile(r, [], True, None)["status"] == "job failing"


def test_tile_last_run():
    ts = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc).timestamp()
    assert usage.tile(row(), [], False, {"finished_at": ts, "ok": True}, timezone.utc)["last_run"] == "ok 09:30"
    assert usage.tile(row(), [], False, {"finished_at": ts, "ok": False}, timezone.utc)["last_run"] == "failed 09:30"
