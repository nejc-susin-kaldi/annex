from annex_ops import messages as m

USERS = {"sam": {"email": "sam@annex.example", "groups": ["builders"]},
         "cm": {"email": "cm@annex.example", "groups": ["users"]},
         "kaldi-admin": {"email": "admin@annex.example", "groups": ["admins", "builders"]}}


def rec(kind="app", owner="sam", alerts_to=None, slack=False):
    return m.recipients(kind, owner, alerts_to, USERS, slack, "#annex-alerts")


def test_app_problems_go_to_the_owner_and_admins():
    emails, channels, notes = rec()
    assert emails == ["sam@annex.example", "admin@annex.example"] and channels == [] and notes == []


def test_platform_problems_go_to_admins_only():
    emails, _, _ = rec("platform", owner="sam")
    assert emails == ["admin@annex.example"]


def test_alerts_to_adds_logins_and_channels():
    emails, channels, _ = rec(alerts_to="owner,cm,#finance-alerts", slack=True)
    assert emails == ["sam@annex.example", "cm@annex.example", "admin@annex.example"]
    assert channels == ["#annex-alerts", "#finance-alerts"]


def test_channels_only_when_slack_is_connected():
    assert rec(alerts_to="owner,#finance-alerts", slack=False)[1] == []
    assert rec(kind="platform", slack=True)[1] == ["#annex-alerts"]


def test_owner_missing_from_the_directory_means_admins_with_a_note():
    emails, _, notes = rec(owner="ghost")
    assert emails == ["admin@annex.example"]
    assert "'ghost' isn't in the user directory" in notes[0]


def test_no_owner_label_and_unknown_extra_login():
    emails, _, notes = rec(owner=None)
    assert emails == ["admin@annex.example"] and "no owner" in notes[0]
    emails, _, notes = rec(alerts_to="owner,nobody")
    assert emails == ["sam@annex.example", "admin@annex.example"] and "'nobody'" in notes[0]


def test_an_admin_owner_is_emailed_once():
    assert rec(owner="kaldi-admin")[0] == ["admin@annex.example"]


def test_subject_lines():
    s = {"down": {}}
    assert m.subject_line("open", "app:client-lookup", s) == "[Annex] client-lookup is down"
    assert m.subject_line("open", "app:x", {"stopped": {}}) == "[Annex] x is down"
    assert m.subject_line("open", "app:x", {"crash-loop": {}}) == "[Annex] x is crash-looping"
    assert m.subject_line("open", "app:x", {"errors": {}}) == "[Annex] x is returning server errors"
    assert m.subject_line("open", "job:monthly-report/monthly", {"job-failed": {}}) == '[Annex] monthly-report: job "monthly" failed'
    assert m.subject_line("open", "platform:gateway", {"stopped": {}}) == "[Annex] platform: gateway stopped"
    assert m.subject_line("open", "platform:front-door", {"down": {}}) == "[Annex] platform: the front door isn't answering"
    assert m.subject_line("remind", "app:x", s) == "[Annex] reminder: x is down"


def test_resolved_subject_lines_with_durations():
    assert m.subject_line("resolve", "app:client-lookup", {"down": {}}, 420) == "[Annex] resolved: client-lookup is back (down 7 min)"
    assert m.subject_line("resolve", "app:x", {"crash-loop": {}}, 45) == "[Annex] resolved: x is back (crash-looping 45 s)"
    assert m.subject_line("resolve", "job:a-b/nightly", {"job-failed": {}}, 5) == '[Annex] resolved: a-b job "nightly" is passing again'
    assert m.subject_line("resolve", "platform:gateway", {"stopped": {}}, 200) == "[Annex] resolved: platform gateway is back (down 3 min)"


def test_duration_formats():
    assert [m.duration(x) for x in (5, 89, 120, 3600, 5400, 7500, 7200)] == ["5 s", "89 s", "2 min", "60 min", "1 h 30 min", "2 h 5 min", "2 h"]


def test_body_lists_signals_links_and_notes():
    text = m.body("open", "app:x", {"down": {"detail": "connection refused"}, "stopped": {"detail": ""}}, 1792072411, 0,
                  ["Note: something"], "https://status.example", "https://x.example")
    assert text.startswith("Since 2026-") and "- down: connection refused" in text
    assert "- stopped: the container is not running" in text
    assert "Status page: https://status.example" in text and "https://x.example" not in text  # the button has the app link
    assert "Logs: annex logs x" in text and "Note: something" in text


def test_resolved_body_has_no_signal_list_and_reminder_says_still_open():
    r = m.body("resolve", "app:x", {"down": {}}, 1792072411, 420, [], "", "")
    assert r.startswith("The problem lasted 7 min") and "What Annex sees" not in r
    assert m.body("remind", "app:x", {"down": {}}, 1792072411, 3700, [], "", "").startswith("Still open since")


def test_platform_body_points_at_container_logs():
    assert "annex-gateway-1" in m.body("open", "platform:gateway", {"stopped": {}}, 0, 0, [], "", "")
