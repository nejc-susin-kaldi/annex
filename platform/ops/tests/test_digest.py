from datetime import datetime, timedelta, timezone

from annex_ops import digest
from annex_ops.usage import UsageSettings

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)  # a Monday
USERS = {"sam": {"email": "sam@annex.example", "groups": ["builders"]},
         "cm": {"email": "cm@annex.example", "groups": ["users"]},
         "kaldi-admin": {"email": "admin@annex.example", "groups": ["admins", "builders"]}}


def row(app, **kw):
    base = {"app": app, "first_seen": NOW - timedelta(days=90), "last_used": NOW - timedelta(days=1), "users_7d": 3,
            "week1": 3, "week2": 4, "week3": 4, "week4": 6, "requests_week": 120, "p95_week": 85, "error_rate_week": 0.0125}
    base.update(kw)
    return base


ROWS = [row("client-lookup"), row("old-tool", last_used=NOW - timedelta(days=60), week1=1, week2=0, week3=0, week4=0, users_7d=0),
        row("quiet", week1=1, week2=1, week3=1, week4=1, users_7d=1)]


def test_trend_numbers_and_change():
    assert digest.trend_numbers(ROWS[0]) == "3 → 4 → 4 → 6"
    assert digest.trend_numbers(row("n", week1=None, week2=None, week3=2, week4=3)) == "- → - → 2 → 3"
    assert digest.change(ROWS[0]) == "+2" and digest.change(ROWS[1]) == "no change" and digest.change(row("n", week3=5, week4=2)) == "-3"
    assert digest.change(row("n", week3=None, week4=2)) == "first week" and digest.change(row("n", week4=None)) == "new"


def test_digest_lists_graduation_and_dormant_sections():
    subject, body, details = digest.render(ROWS, NOW, UsageSettings(), "https://metabase/search?q=Annex%20usage")
    assert "1 to graduate, 1 dormant" in subject
    assert "Graduation candidates" in body and "client-lookup" in body.split("Dormant")[0]
    assert "Dormant (no users in 30 days): old-tool." in body
    assert "https://metabase" in body and "logins aren't shown" in body
    assert "client-lookup" in details and "3 → 4 → 4 → 6" in details and "1.2% errors" in details and "p95 85 ms" in details


def test_digest_without_signals_says_none():
    _, body, _ = digest.render([ROWS[2]], NOW, UsageSettings())
    assert "Graduation candidates" in body and ": none." in body and "Dormant (no users in 30 days): none." in body


def test_digest_dormant_override_for_tests_and_demos():
    idle = row("usage-idle", last_used=None, first_seen=NOW - timedelta(hours=2), users_7d=0, week1=None, week2=None, week3=None, week4=None)
    used = row("client-lookup", last_used=NOW - timedelta(hours=1))
    _, body, _ = digest.render([used, idle], NOW, UsageSettings(), dormant_days=1, min_age_days=0)
    assert "Dormant (no users in 1 days): usage-idle." in body


def test_digest_never_contains_a_login():
    owners = {"client-lookup": "sam", "old-tool": "cm", "quiet": "sam"}
    for addr, rows in digest.audience(ROWS, owners, USERS).items():
        text = " ".join(digest.render(rows, NOW, UsageSettings(), "https://x")[1:]) + digest.render(rows, NOW, UsageSettings())[0]
        for login in USERS:
            assert login not in text.replace("annex.example", ""), (addr, login)


def test_admins_get_every_app_and_owners_their_own():
    owners = {"client-lookup": "sam", "old-tool": "cm", "quiet": "sam"}
    got = digest.audience(ROWS, owners, USERS)
    assert {r["app"] for r in got["admin@annex.example"]} == {"client-lookup", "old-tool", "quiet"}
    assert {r["app"] for r in got["sam@annex.example"]} == {"client-lookup", "quiet"}
    assert {r["app"] for r in got["cm@annex.example"]} == {"old-tool"}


def test_an_admin_who_owns_apps_gets_one_digest_with_each_app_once():
    got = digest.audience(ROWS, {"client-lookup": "kaldi-admin"}, USERS)
    assert [r["app"] for r in got["admin@annex.example"]].count("client-lookup") == 1 and len(got) == 1


def test_unknown_or_missing_owners_mean_admins_only():
    got = digest.audience(ROWS, {"client-lookup": "ghost", "old-tool": None}, USERS)
    assert list(got) == ["admin@annex.example"]


def test_digest_schedule():
    mon8 = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
    week = mon8.date()
    assert not digest.due(mon8 - timedelta(minutes=1), 0, 8, set())
    assert digest.due(mon8, 0, 8, set())
    assert digest.due(mon8 + timedelta(hours=23, minutes=59), 0, 8, set())   # a restart later that day still sends it
    assert not digest.due(mon8 + timedelta(hours=24), 0, 8, set())            # but a fresh install on Tuesday doesn't
    assert not digest.due(mon8 + timedelta(days=2), 0, 8, set())
    assert not digest.due(mon8, 0, 8, {week})                                  # once per week
    assert digest.due(mon8 + timedelta(days=7), 0, 8, {week})                  # next Monday is a new week
    assert not digest.due(mon8 - timedelta(hours=1), 0, 8, set())              # Sunday night: not yet
    assert digest.due(datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc), 2, 9, set())  # configurable: Wednesday 09:00
