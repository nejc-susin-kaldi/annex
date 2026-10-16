"""The console's rules and rendering (M2.4): who sees what, and that nothing from an app can inject markup."""
from datetime import datetime, timezone

from annex_ops import console_view as v


def test_who_comes_from_the_forward_auth_headers_only():
    assert v.parse_who({}) is None and v.parse_who({"remote-user": "  "}) is None
    w = v.parse_who({"remote-user": "sam", "remote-groups": "builders, users"})
    assert (w.login, w.groups, w.admin) == ("sam", ["builders", "users"], False)
    assert v.parse_who({"remote-user": "kaldi-admin", "remote-groups": "admins,builders"}).admin


def test_an_app_is_for_its_owner_and_the_admins():
    sam, cm, admin = v.Who("sam", ["builders"]), v.Who("cm", ["users"]), v.Who("kaldi-admin", ["admins"])
    assert v.can_see_app(sam, "sam") and v.can_see_app(admin, "sam") and v.can_see_app(admin, None)
    assert not v.can_see_app(cm, "sam") and not v.can_see_app(sam, "cm")
    assert not v.can_see_app(sam, None) and not v.can_see_app(sam, "")   # an app with no known owner is for admins only


def test_a_user_named_admins_is_not_an_admin_and_a_group_must_match_exactly():
    assert not v.Who("admins", ["users"]).admin and not v.Who("x", ["admins-ish", "superadmins"]).admin


def test_everything_from_an_app_is_escaped():
    who = v.Who("sam", ["builders"])
    evil = '<script>alert("x")</script>'
    row = {"ts": "2026-10-16T10:00:00.123456Z", "container": "annex-app-x", "release": 3, "level": "error", "request_id": "r1",
           "msg": evil, "fields": {"k": evil, "stack": evil}}
    out = v.logs_page(who, "x", [row], {"grep": evil, "level": "", "release": evil, "request": evil}, older=None)
    assert "<script>" not in out and "&lt;script&gt;" in out
    d = {"app": "x", "owner": evil, "live": 3, "manifest": {"type": "web", "description": evil, "env": {"A": evil}, "secrets": ["S"]},
         "health": {"state": "down", "detail": evil}, "incidents": [{"opened_at": "t", "subject": "app:x", "signals": {"down": evil}}],
         "jobs": [], "usage": None, "warnings": [evil], "releases": [{"release": 3, "result": "live", "live": True, "deployed_at": "t",
         "deployed_by": evil, "via": "cli", "git": None, "note": evil}],
         "errors": [{"state": "active", "kind": "server", "title": evil, "count": 1, "last_seen": "2026-10-16T10:00:00Z", "first_release": 3,
                     "last_release": 3, "user": evil, "request_id": evil}]}
    page = v.app_page(who, d)
    assert "<script>" not in page and page.count("&lt;script&gt;") >= 7


def test_the_page_says_it_is_read_only_and_shows_the_cli_command():
    page = v.app_page(v.Who("sam", []), {"app": "x", "owner": "sam", "live": 1, "manifest": None, "health": {"state": "up"}, "incidents": [],
                                         "jobs": [], "usage": None, "warnings": [], "releases": [], "errors": []})
    assert "Read-only" in page and "annex rollback x" in page and "annex errors x" in page


def test_per_person_usage_is_only_in_the_admins_page():
    u = {"app": {"users_7d": 1, "users_prev_7d": 0, "trend": "↑1", "users_30d": 1, "requests_7d": 5, "errors_7d": 0, "p95_7d": 10, "signal": None,
                 "week1": None, "week2": None, "week3": None, "week4": 1}, "days": [], "by_user": [{"login": "sam-secret-login", "requests": 5}]}
    base = {"app": "x", "owner": "sam", "live": 1, "manifest": None, "health": {"state": "up"}, "incidents": [], "jobs": [], "usage": u,
            "warnings": [], "releases": [], "errors": []}
    assert "sam-secret-login" in v.app_page(v.Who("kaldi-admin", ["admins"]), base)
    assert "sam-secret-login" not in v.app_page(v.Who("sam", ["builders"]), {**base, "usage": {**u, "by_user": None}})


def test_injected_names_never_include_values_and_follow_the_manifest():
    m = {"type": "web+job", "postgres": True, "files": True, "views": ["a"], "notify_email": True, "webhooks": [{"name": "x"}], "secrets": ["MY_KEY"],
         "env": {"LOG_LEVEL": "debug"}}
    names = v.injected_names(m)
    assert {"PORT", "DATABASE_URL", "DATA_DIR", "ANNEX_GATEWAY_TOKEN", "ANNEX_OPS_TOKEN", "ANNEX_HOOK_TOKEN", "MY_KEY", "LOG_LEVEL"} <= set(names)
    assert "ANNEX_GATEWAY_URL" in names and v.injected_names({"type": "job"}) == ["ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL"]


def test_the_status_page_counts_what_is_down():
    who = v.Who("cm", ["users"])
    ok = v.status_page(who, [{"app": "a", "state": "up"}], [{"name": "front door", "state": "up"}])
    assert "Everything is up" in ok
    bad = v.status_page(who, [{"app": "a", "state": "down", "detail": "x"}], [])
    assert "1 thing down" in bad and "a" in bad


def test_a_user_with_no_apps_gets_a_pointer_to_the_status_page_not_an_error():
    page = v.fleet_page(v.Who("cm", ["users"]), [], [], None)
    assert "don't own any apps" in page and "/status" in page


def test_when_formats_local_time():
    assert v.when(datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)) == "2026-10-16 12:00"
    assert v.when(None) == ""
