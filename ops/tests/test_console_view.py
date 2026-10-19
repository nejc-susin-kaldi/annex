"""The console's rules and rendering (M2.4, M2.7-M2.9): who sees what, that nothing from an app can inject markup, and the
pure rules behind the overview (severity, the attention list, day series)."""
from datetime import date, datetime, timedelta, timezone

from annex_ops import console_data as data, console_pages_admin as pg, console_pages_app as pa, console_pages_overview as po, console_view as v

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
ADMIN, SAM = v.Who("kaldi-admin", ["admins"]), v.Who("sam", ["builders"])
EVIL = '<script>alert("x")</script>'


def row(app="x", **kw):
    base = {"app": app, "owner": "sam", "health": "up", "health_detail": "", "users_7d": 3, "trend": "=", "spark": [1] * 14, "open_errors": 0, "new_errors": 0,
            "live": 2, "deployed_at": NOW - timedelta(days=2), "signal": None, "incident": False, "failing_jobs": [], "failed_release": None, "warnings": []}
    return {**base, **kw}


def detail(**kw):
    d = {"app": "x", "owner": "sam", "live": 3, "manifest": {"type": "web", "memory_mb": 256}, "health": {"state": "up", "detail": ""}, "incidents": [], "jobs": [],
         "usage": None, "series": {"users": [0] * 30, "requests": [0] * 30, "errors": [0] * 30, "days": ["d"] * 30}, "errors": [], "active_errors": 0, "releases": [],
         "warnings": [], "recent": [], "failed_release": None}
    return {**d, **kw}


# --- who sees what ---------------------------------------------------------------------------------------------
def test_who_comes_from_the_forward_auth_headers_only():
    assert v.parse_who({}) is None and v.parse_who({"remote-user": "  "}) is None
    w = v.parse_who({"remote-user": "sam", "remote-groups": "builders, users"})
    assert (w.login, w.groups, w.admin) == ("sam", ["builders", "users"], False)
    assert v.parse_who({"remote-user": "kaldi-admin", "remote-groups": "admins,builders"}).admin


def test_an_app_is_for_its_owner_and_the_admins():
    cm = v.Who("cm", ["users"])
    assert v.can_see_app(SAM, "sam") and v.can_see_app(ADMIN, "sam") and v.can_see_app(ADMIN, None)
    assert not v.can_see_app(cm, "sam") and not v.can_see_app(SAM, "cm")
    assert not v.can_see_app(SAM, None) and not v.can_see_app(SAM, "")   # an app with no known owner is for admins only


def test_a_user_named_admins_is_not_an_admin_and_a_group_must_match_exactly():
    assert not v.Who("admins", ["users"]).admin and not v.Who("x", ["admins-ish", "superadmins"]).admin


def test_the_nav_gives_admins_activity_and_platform_and_others_status():
    assert [n for n, _, _ in v.nav_links(ADMIN)] == ["overview", "apps", "activity", "platform"]
    assert [n for n, _, _ in v.nav_links(SAM)] == ["overview", "apps", "status"]


# --- nothing from an app is markup -------------------------------------------------------------------------------
def test_everything_from_an_app_is_escaped():
    log = {"ts": "2026-10-16T10:00:00.123456Z", "container": "annex-app-x", "release": 3, "level": "error", "request_id": "r1", "msg": EVIL,
           "fields": {"k": EVIL, "stack": EVIL}}
    d = detail(owner=EVIL, manifest={"type": "web", "description": EVIL, "env": {"A": EVIL}, "secrets": ["S"]}, health={"state": "down", "detail": EVIL},
               incidents=[{"opened_at": "t", "subject": "app:x", "signals": {"down": EVIL}}], warnings=[EVIL], recent=[log],
               jobs=[{"job": EVIL, "last_run": "2026-10-16 10:00", "last_run_raw": "2026-10-16T10:00:00+00:00", "ok": False, "duration_ms": 1, "error": EVIL}],
               releases=[{"release": 3, "result": "live", "live": True, "deployed_at_raw": NOW, "deployed_by": EVIL, "via": "cli", "git": None, "note": EVIL}],
               errors=[{"state": "active", "kind": "server", "title": EVIL, "count": 1, "first_seen": "2026-10-16T10:00:00Z", "last_seen": "2026-10-16T10:00:00Z",
                        "first_release": 3, "last_release": 3, "user": EVIL, "request_id": EVIL}])
    att = data.app_attention(d, NOW)
    pages = [pa.summary_page(SAM, d, att, now=NOW), pa.errors_page(SAM, d, "all", now=NOW), pa.releases_page(SAM, d, now=NOW), pa.config_page(SAM, d),
             pa.logs_page(SAM, d, [log], {"grep": EVIL, "level": "", "release": EVIL, "request": EVIL}, None)]
    for page in pages:
        assert "<script>" not in page, page[:200]
    assert sum(p.count("&lt;script&gt;") for p in pages) >= 12
    # the overview, apps list, status page, activity and platform: every name and detail an app can influence
    r = row(owner=EVIL, health="down", health_detail=EVIL, failing_jobs=[EVIL])
    ov = {"rows": [r], "platform": [{"subject": "platform:" + EVIL, "opened_at": "2026-10-16T10:00:00+00:00", "what": EVIL}], "host": None, "front_door": "up",
          "attention": data.attention_items([r], [{"subject": "platform:" + EVIL, "what": EVIL}], False, None), "stale": False, "checked_at": None,
          "kpis": {"apps": 1, "apps_up": 0, "people_7d": 0, "people_change": 0, "open_errors": 0, "deploys_7d": 0, "deploys_failed_7d": 0, "host": None},
          "feed": [{"ts": NOW, "actor": EVIL, "app": EVIL, "action": EVIL, "details": {EVIL: EVIL}, "kind": "audit"}]}
    out = [po.overview_page(ADMIN, ov), po.apps_page(ADMIN, ov, "All", EVIL), po.status_page(SAM, [{"app": EVIL, "state": "down", "detail": EVIL}], []),
           pg.activity_page(ADMIN, ov["feed"], False, {"app": EVIL, "actor": EVIL, "chip": ""}, list(data.CHIPS), NOW)]
    for page in out:
        assert "<script>" not in page


# --- read-only and the CLI block ---------------------------------------------------------------------------------
def test_each_page_says_once_that_it_is_read_only_and_shows_the_cli_command():
    page = pa.summary_page(SAM, detail(), [], now=NOW)
    assert page.count("This console is read-only") == 1 and "annex rollback x" in page and "annex status x" in page
    assert "annex errors x" in pa.errors_page(SAM, detail(), now=NOW) and "annex releases x" in pa.releases_page(SAM, detail(), now=NOW)


# --- usage -------------------------------------------------------------------------------------------------------
def usage_detail(by_user):
    a = {"users_7d": 1, "users_prev_7d": 0, "trend": "↑1", "users_30d": 1, "requests_7d": 5, "errors_7d": 0, "p95_7d": 10, "signal": None, "week1": None,
         "week2": None, "week3": None, "week4": 1}
    return detail(usage={"app": a, "days": [], "by_user": by_user})


def test_per_person_usage_is_only_in_the_admins_page():
    d = usage_detail([{"login": "sam-secret-login", "requests": 5}])
    assert "sam-secret-login" in pa.usage_page(ADMIN, d) and "Personal data" in pa.usage_page(ADMIN, d)
    assert "sam-secret-login" not in pa.usage_page(SAM, usage_detail(None))
    assert "sam-secret-login" not in pa.usage_page(SAM, d), "even if the data were passed in, an owner's page doesn't print it"


def test_the_usage_page_says_the_numbers_the_cli_says():
    assert "1 user(s) in the last 7 days (↑1), 1 in 30 days, 5 requests" in pa.usage_page(SAM, usage_detail(None))
    assert "No usage recorded yet" in pa.usage_page(SAM, detail())


def test_injected_names_never_include_values_and_follow_the_manifest():
    m = {"type": "web+job", "postgres": True, "files": True, "views": ["a"], "notify_email": True, "webhooks": [{"name": "x"}], "secrets": ["MY_KEY"],
         "env": {"LOG_LEVEL": "debug"}}
    names = pa.injected_names(m)
    assert {"PORT", "DATABASE_URL", "DATA_DIR", "ANNEX_GATEWAY_TOKEN", "ANNEX_OPS_TOKEN", "ANNEX_HOOK_TOKEN", "MY_KEY", "LOG_LEVEL"} <= set(names)
    assert "ANNEX_GATEWAY_URL" in names and pa.injected_names({"type": "job"}) == ["ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL"]


def test_the_config_page_groups_the_manifest_and_shows_no_values_of_secrets():
    page = pa.config_page(SAM, detail(manifest={"type": "web", "secrets": ["MY_KEY"], "views": ["orders_v"], "notify_slack": ["#x"]}))
    for group in ("Runtime", "Storage", "Data access", "Notifications", "Webhooks", "Secrets and environment"):
        assert f"<h2>{group}</h2>" in page
    assert "MY_KEY" in page and "orders_v" in page and "#x" in page


# --- the overview ------------------------------------------------------------------------------------------------
def test_the_status_page_counts_what_is_down_and_says_when_the_checker_is_silent():
    ok = po.status_page(v.Who("cm", ["users"]), [{"app": "a", "state": "up"}], [{"name": "front door", "state": "up"}])
    assert "Everything is up" in ok
    bad = po.status_page(v.Who("cm", ["users"]), [{"app": "a", "state": "down", "detail": "x"}], [])
    assert "1 thing down" in bad and "a" in bad
    assert "isn't answering" in po.status_page(v.Who("cm", ["users"]), [{"app": "a", "state": "up"}], [], stale=True)


def test_a_user_with_no_apps_gets_a_pointer_to_the_status_page_not_an_error():
    ov = {"rows": [], "attention": [], "kpis": {}, "platform": [], "host": None, "feed": []}
    for page in (po.overview_page(v.Who("cm", ["users"]), ov), po.apps_page(v.Who("cm", ["users"]), ov)):
        assert "don't own any apps" in page and "/status" in page


def test_severity_orders_down_before_incident_before_unknown_before_errors():
    order = [row("a", health="down"), row("b", incident=True), row("c", failing_jobs=["j"]), row("d", health="unknown"), row("e", open_errors=1),
             row("f", warnings=["w"]), row("g", signal="dormant"), row("h")]
    assert [data.severity(r) for r in order] == [0, 1, 1, 2, 3, 3, 4, 5]
    assert [data.needs_attention(r) for r in order] == [True] * 6 + [False, False]


def test_the_attention_list_is_ranked_and_each_item_says_where_to_go():
    rows = [row("ok-app"), row("down-app", health="down", health_detail="refused"), row("job-app", failing_jobs=["nightly"]),
            row("err-app", new_errors=2, open_errors=2), row("rel-app", failed_release="3 h ago")]
    items = data.attention_items(rows, [{"subject": "platform:backup", "what": "overdue"}], False, None)
    texts = [i["text"] for i in items]
    assert texts[0] == "down-app is down: refused" and items[0]["href"] == "/apps/down-app"
    assert "backup: overdue" in texts and "job-app: job nightly failed its last run" in texts
    assert "err-app: 2 new error groups since yesterday" in texts and "rel-app: a release failed 3 h ago" in texts
    assert not any("ok-app" in t for t in texts)
    assert [i["sev"] for i in items] == sorted(i["sev"] for i in items)


def test_a_silent_checker_is_an_attention_item_and_never_all_clear():
    items = data.attention_items([row()], [], True, 600)
    assert items and "health checker hasn't reported for 10 min" in items[0]["text"]
    ov = {"rows": [row()], "platform": [], "host": None, "front_door": "up", "attention": [], "stale": False, "checked_at": NOW.timestamp(), "feed": [],
          "kpis": {"apps": 1, "apps_up": 1, "people_7d": 1, "people_change": 0, "open_errors": 0, "deploys_7d": 0, "deploys_failed_7d": 0, "host": None}}
    page = po.overview_page(SAM, ov)
    assert "All clear" in page and "Needs attention" not in page


def test_checker_stale_needs_three_intervals_and_at_least_the_minimum():
    assert data.checker_stale(None, 1000, 60)
    assert not data.checker_stale(1000 - 170, 1000, 60) and data.checker_stale(1000 - 200, 1000, 60)
    assert not data.checker_stale(1000 - 250, 1000, 100) and data.checker_stale(1000 - 310, 1000, 100)


def test_an_owner_sees_no_platform_or_activity_in_the_overview():
    ov = {"rows": [row()], "platform": [], "host": None, "front_door": "up", "attention": [], "stale": False, "checked_at": None, "feed": [],
          "kpis": {"apps": 1, "apps_up": 1, "people_7d": 1, "people_change": 0, "open_errors": 0, "deploys_7d": 0, "deploys_failed_7d": 0, "host": None}}
    page = po.overview_page(SAM, ov)
    assert "Platform details" not in page and "Latest activity" not in page and "Disk used" not in page
    assert "Platform details" in po.overview_page(ADMIN, {**ov, "feed": []}) and "Memory available" not in page


def test_the_apps_list_filters_and_counts():
    rows = [row("a", health="down"), row("b", signal="dormant"), row("c", signal="graduation", owner="kaldi-admin"), row("d")]
    ov = {"rows": rows}
    assert [r["app"] for r in po.filter_rows(rows, ADMIN, "Needs attention", "")] == ["a"]
    assert [r["app"] for r in po.filter_rows(rows, ADMIN, "Dormant", "")] == ["b"]
    assert [r["app"] for r in po.filter_rows(rows, ADMIN, "Graduating", "")] == ["c"]
    assert [r["app"] for r in po.filter_rows(rows, ADMIN, "Mine", "")] == ["c"]
    assert [r["app"] for r in po.filter_rows(rows, ADMIN, "All", "SAM")] == ["a", "b", "d"]
    page = po.apps_page(ADMIN, ov, "Dormant")
    assert 'class="fc on"' in page and "Needs attention" in page and ">b<" in page and ">a<" not in page


def test_the_apps_table_has_one_sparkline_per_app_and_relative_deploy_times():
    page = po.apps_page(SAM, {"rows": [row("x", live=7)]})
    assert page.count('class="spark"') == 1 and "r7 · " in page and "<time" in page


# --- an app's Summary -----------------------------------------------------------------------------------------------
def test_app_attention_merges_incident_jobs_warnings_and_errors_without_repeating_a_job():
    d = detail(health={"state": "down", "detail": "refused"}, incidents=[{"opened_at": "t", "subject": "app:x", "signals": {"errors": "5 in 5 min"}}],
               jobs=[{"job": "nightly", "ok": False, "error": "timeout", "last_run": "x", "last_run_raw": "x", "duration_ms": 1}],
               warnings=["job nightly failed its last run (2026)", "logs aren't structured: the app prints plain text"],
               errors=[{"state": "active", "title": "Boom", "count": 4, "first_release": 2}, {"state": "quiet", "title": "Old", "count": 1, "first_release": 1}],
               failed_release={"release": 5, "when": "3 h ago"})
    texts = [i["text"] for i in data.app_attention(d, NOW)]
    assert texts[0].startswith("Down: refused") and any("Open incident" in t for t in texts)
    assert sum("nightly" in t for t in texts) == 1 and "Job nightly failed its last run: timeout" in texts
    assert any("logs aren't structured" in t for t in texts) and "Boom (4 times, since r2)" in texts and not any("Old" in t for t in texts)
    assert "Release r5 failed 3 h ago" in texts
    assert data.app_attention(detail(), NOW) == []


def test_the_summary_says_when_nothing_needs_attention_and_shows_the_header_on_every_tab():
    assert "Nothing needs attention" in pa.summary_page(SAM, detail(), [], now=NOW)
    for page in (pa.summary_page(SAM, detail(), [], now=NOW), pa.errors_page(SAM, detail(), now=NOW), pa.releases_page(SAM, detail(), now=NOW), pa.config_page(SAM, detail())):
        assert '<nav class="tabs"' in page and "owner sam" in page and "live r3" in page
        for tab in ("Summary", "Logs", "Errors", "Releases", "Usage", "Config"):
            assert f">{tab}" in page
    assert page.count('aria-current="page"') == 2   # the nav's tab and the app's tab


def test_the_errors_tab_filters_by_state():
    groups = [{"state": "active", "kind": "server", "title": "Live one", "count": 1, "first_seen": "2026-10-16T10:00:00Z", "last_seen": "2026-10-16T10:00:00Z",
               "first_release": 3, "last_release": 3, "user": None, "request_id": None},
              {"state": "quiet", "kind": "server", "title": "Old one", "count": 1, "first_seen": "2026-10-10T10:00:00Z", "last_seen": "2026-10-10T10:00:00Z",
               "first_release": 1, "last_release": 2, "user": None, "request_id": None}]
    d = detail(errors=groups, active_errors=1)
    assert "Live one" in pa.errors_page(SAM, d) and "Old one" not in pa.errors_page(SAM, d)
    assert "Old one" in pa.errors_page(SAM, d, "quiet") and "Live one" in pa.errors_page(SAM, d, "all")
    assert "no errors seen" in pa.errors_page(SAM, detail())
    assert "r1–r2" in pa.errors_page(SAM, d, "all")


# --- time, states, charts ------------------------------------------------------------------------------------------
def test_when_formats_local_time():
    assert v.when(datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)) == "2026-10-16 12:00"
    assert v.when(None) == ""


def test_relative_time_has_the_absolute_time_on_hover():
    out = str(v.rel_time(NOW - timedelta(days=3), NOW))
    assert ">3 d ago<" in out and 'title="2026-10-16 14:00"' in out
    assert ">just now<" in str(v.rel_time(NOW, NOW)) and ">5 min ago<" in str(v.rel_time(NOW - timedelta(minutes=5), NOW))
    assert ">2026-06-01<" in str(v.rel_time(datetime(2026, 6, 1, tzinfo=timezone.utc), NOW))
    assert v.rel_time(None) == "" and ">1 d ago<" in str(v.rel_time("2026-10-18T12:00:00Z", NOW))


def test_every_state_word_maps_onto_four_states():
    for word, want in [("up", "ok"), ("live", "ok"), ("down", "bad"), ("failed", "bad"), ("active", "bad"), ("unknown", "warn"), ("quiet", "off"), ("superseded", "off")]:
        assert v.state_class(word) == want
    assert v.state_class("never-heard-of-it") == "off"
    assert 'class="st bad"' in str(v.state("down"))


def test_charts_are_inline_svg_with_text_alternatives_and_survive_empty_data():
    s = str(v.sparkline([1, 3, 2], "users per day"))
    assert s.startswith("<svg") and 'aria-label="users per day: 1, 3, 2"' in s and "<polyline" in s
    assert "<svg" in str(v.sparkline([])) and "<svg" in str(v.sparkline([0, 0, 0])) and "<svg" in str(v.sparkline([5]))
    b = str(v.bars([1, 2, 0], ["a", "b", "c"], [0, 1, 0], unit="users"))
    assert b.count("<rect") == 4 and "b: 2 users, 1 errors" in b
    assert "no data yet" in str(v.bars([], []))
    assert "script" not in b and "script" not in s


def test_fill_days_pads_missing_days_with_zero_oldest_first():
    today = date(2026, 10, 19)
    assert data.fill_days({date(2026, 10, 19): 4, date(2026, 10, 17): 2}, 4, today) == [0, 2, 0, 4]
    assert data.fill_days({}, 3, today) == [0, 0, 0]


def test_host_url_follows_the_traefik_pattern(monkeypatch):
    monkeypatch.setenv("ANNEX_HOST_PATTERN", "{name}.annex.home.arpa")
    monkeypatch.setenv("ANNEX_PORT_SUFFIX", ":8443")
    assert v.host_url("logs") == "https://logs.annex.home.arpa:8443" and v.host_url("a/b") is None
    monkeypatch.setenv("ANNEX_HOST_PATTERN", "")
    assert v.host_url("logs") is None


# --- activity and platform ---------------------------------------------------------------------------------------
def test_activity_has_chips_filters_links_and_paging():
    ev = [{"ts": NOW - timedelta(hours=1), "actor": "sam@mac", "app": "x", "action": "deploy", "details": {"release": 2}, "kind": "audit"},
          {"ts": NOW - timedelta(hours=2), "actor": "sam@mac", "app": "x", "action": "deploy.failed", "details": {"error": "e" * 300}, "kind": "audit"},
          {"ts": NOW - timedelta(hours=3), "actor": None, "app": "x", "action": "incident.opened", "details": {"subject": "app:x", "signals": "down"}, "kind": "platform"}]
    page = pg.activity_page(ADMIN, ev, True, {"chip": "Deploys", "app": "x"}, list(data.CHIPS), NOW)
    for chip in data.CHIPS:
        assert f">{chip}" in page
    assert 'class="fc on"' in page and "/apps/x" in page and "actor=sam%40mac" in page and "Older" in page and "before=" in page
    assert '<details class="more">' in page          # the long error folds
    assert "incident.opened" in page and "platform" in page
    assert "Older" not in pg.activity_page(ADMIN, ev, False, {"chip": ""}, list(data.CHIPS), NOW)


def test_the_platform_page_shows_the_numbers_the_thresholds_and_stale_samples():
    t = {"disk_alert_pct": 85, "disk_clear_pct": 80, "mem_alert_mb": 256, "mem_clear_mb": 400, "backup_max_age_s": 93600}
    h = {"ts": NOW.timestamp() - 120, "disk_used_pct": 90.0, "disk_free_gb": 3.0, "mem_available_mb": 100.0, "backup_at": NOW.timestamp() - 2 * 86400, "stale": False}
    p = {"front_door": "up", "incidents": [], "host": h, "series": {"disk": [1, 2], "mem": [3, 4]}, "tuning": t, "links": [("logs", "https://logs.x", "live logs")]}
    page = pg.platform_page(ADMIN, p, NOW.timestamp())
    assert "90 %" in page and "alert at 85 %" in page and "100 MB" in page and "48 h old" in page and 'class="meter bad"' in page
    assert 'href="https://logs.x"' in page and "Sampled 2 min ago" in page
    assert "not sampled recently" in pg.platform_page(ADMIN, {**p, "host": {**h, "stale": True}}, NOW.timestamp())
    assert "No host samples yet" in pg.platform_page(ADMIN, {**p, "host": None}, NOW.timestamp())


def test_the_releases_tab_shows_the_newest_25_and_can_show_all():
    rels = [{"release": n, "result": "live", "live": n == 40, "deployed_at_raw": NOW - timedelta(days=40 - n), "deployed_by": "x", "via": "cli", "git": None, "note": None}
            for n in range(40, 0, -1)]
    d = detail(releases=rels)
    page = pa.releases_page(SAM, d, now=NOW)
    assert page.count("/logs?release=") == 25 and "of 40" in page and "releases?all=1" in page
    assert pa.releases_page(SAM, d, now=NOW, show_all=True).count("/logs?release=") == 40
