"""Error groups (M2.3): what is "the same error", when an email is owed. Pure functions."""
from datetime import datetime, timedelta, timezone

from annex_ops import errors_group as g

T0 = datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc)


def row(msg, i=0, level=None, fields=None, app="x", container="annex-app-x", release=3, h=None, rid=None, gap=0.001):
    return {"app": app, "container": container, "ts": T0 + timedelta(seconds=i * gap), "ns": 0, "msg": msg, "level": level,
            "fields": fields, "release": release, "request_id": rid, "h": h or f"h{i}-{msg[:6]}"}


def test_numbers_ids_strings_and_paths_become_placeholders():
    assert g.normalise("cannot load client 123 for 'Novak d.o.o.'") == "cannot load client <n> for <str>"
    assert g.normalise("no row 8f3c2a1e-1b2c-4d5e-8f90-123456789abc") == "no row <uuid>"
    assert g.normalise("open /srv/annex/apps/x/data/a.csv failed") == "open <path> failed"
    assert g.normalise("token deadbeef12345678 expired after 30.5 s") == "token <hex> expired after <n> s"
    assert g.normalise("a   b\n c") == "a b c"


def test_the_same_error_with_other_values_is_one_group_and_other_errors_are_not():
    a = g.fingerprint("x", g.normalise("cannot load client 123"))
    assert a == g.fingerprint("x", g.normalise("cannot load client 987"))
    assert a != g.fingerprint("x", g.normalise("cannot save client 123"))
    assert a != g.fingerprint("y", g.normalise("cannot load client 123"))   # per app


def test_a_structured_exception_is_its_type_and_last_frame_whatever_the_message():
    stack = 'Traceback (most recent call last):\n  File "/app/main.py", line 40, in index\n    f()\n  File "/app/main.py", line 12, in load\n    1/0\nZeroDivisionError: division by zero'
    f1 = {"exc": "ZeroDivisionError: division by zero", "stack": stack, "user": "sam", "method": "GET"}
    f2 = {"exc": "ZeroDivisionError: integer division 7", "stack": stack, "user": "cm"}
    e1, _ = g.assemble([row("unhandled error", 0, "error", f1, rid="r1")])
    e2, _ = g.assemble([row("something else entirely", 1, "error", f2)])
    assert e1[0].fingerprint == e2[0].fingerprint
    assert e1[0].title == "ZeroDivisionError in load" and e1[0].user == "sam" and e1[0].request_id == "r1" and e1[0].release == 3
    assert e1[0].kind == "server"


def test_a_structured_error_without_a_stack_is_its_normalised_message():
    ev, _ = g.assemble([row("could not load clients", 0, "error", {"status": 502}), row("could not load clients", 1, "error", {"status": 503})])
    assert len(ev) == 2 and ev[0].fingerprint == ev[1].fingerprint and ev[0].title == "could not load clients"


def test_info_and_warn_lines_are_not_events():
    ev, _ = g.assemble([row("fine", 0, "info", {}), row("odd", 1, "warn", {}), row("plain line", 2)])
    assert ev == []


def test_a_printed_traceback_is_one_event():
    rows = [row("Traceback (most recent call last):", 0, "error"),
            row('  File "/app/probe.py", line 31, in do_GET', 1),
            row("    1 / 0", 2),
            row('  File "/app/probe.py", line 20, in compute', 3, "error"),
            row("    1 / 0", 4),
            row("ZeroDivisionError: division by zero", 5, "error"),
            row("something later", 5000, "info", {})]
    ev, open_ = g.assemble(rows)
    assert len(ev) == 1 and open_ == {}
    assert ev[0].title == "ZeroDivisionError in compute" and ev[0].sample == "ZeroDivisionError: division by zero"
    assert ev[0].h == rows[0]["h"]
    # the same trace with another message and other line numbers is the same group
    rows2 = [dict(r, h=r["h"] + "b") for r in rows]
    rows2[5]["msg"] = "ZeroDivisionError: other words 99"
    assert g.assemble(rows2)[0][0].fingerprint == ev[0].fingerprint


def test_a_chained_trace_is_one_event_about_the_last_exception():
    rows = [row("Traceback (most recent call last):", 0, "error"), row('  File "/a/x.py", line 1, in first', 1),
            row("KeyError: 'k'", 2, "error"),
            row("During handling of the above exception, another exception occurred:", 3),
            row("Traceback (most recent call last):", 4, "error"), row('  File "/a/x.py", line 9, in second', 5),
            row("RuntimeError: wrapped", 6, "error")]
    ev, _ = g.assemble(rows + [row("after", 9000)])
    assert len(ev) == 1 and ev[0].title == "RuntimeError in second"


def test_a_trace_cut_by_the_end_of_the_window_is_held_back():
    rows = [row("Traceback (most recent call last):", 0, "error"), row('  File "/a/x.py", line 1, in f', 1)]
    ev, open_ = g.assemble(rows)
    assert ev == [] and open_ == {"annex-app-x": rows[0]["ts"].timestamp()}


def test_error_lines_of_two_containers_dont_mix_and_jobs_and_browser_are_kinds():
    rows = [row("boom 1", 0, "error", {}, container="job:tick"),
            row("browser error: x is not defined", 1, "error", {"source": "browser", "user": "sam"}, container="annex-app-x"),
            row("boom 2", 2, "error", {}, app="y", container="annex-app-y")]
    ev, _ = g.assemble(rows)
    assert [(e.kind, e.app) for e in ev] == [("job", "x"), ("browser", "x"), ("server", "y")]


def test_when_an_email_is_owed():
    prev = {"last_seen": 1000.0, "last_release": 3}
    assert g.should_alert(None, 1000, 3, 86400)                      # a new group
    assert not g.should_alert(prev, 1001, 3, 86400)                  # more of the same
    assert g.should_alert(prev, 1000 + 86401, 3, 86400)              # after a quiet day
    assert g.should_alert(prev, 1010, 4, 86400)                      # in a newer release
    assert not g.should_alert(prev, 1010, 2, 86400)                  # an old release's late line
    assert not g.should_alert(prev, 1010, None, 86400)


def test_active_or_quiet():
    assert g.group_state(1000, 3, 3, 1100, 86400) == "active"
    assert g.group_state(1000, 3, 4, 1100, 86400) == "quiet"         # a newer release went live and it hasn't come back
    assert g.group_state(1000, 3, 3, 1000 + 86401, 86400) == "quiet"
    assert g.group_state(1000, None, None, 1100, 86400) == "active"


def test_the_hourly_limit_sends_the_rest_as_one_summary():
    due = [{"n": i} for i in range(8)]
    assert g.plan_alerts(due, 0, 5) == (due[:4], due[4:])            # four emails and one summary of the other four
    assert g.plan_alerts(due[:3], 0, 5) == (due[:3], [])
    assert g.plan_alerts(due, 5, 5) == ([], [])                      # none left this hour: they wait
    assert g.plan_alerts(due, 4, 5) == ([], due)                     # the last slot is the summary
    assert g.plan_alerts([], 0, 5) == ([], [])


def test_the_messages():
    grp = {"title": "ZeroDivisionError in compute", "kind": "server", "last_release": 7, "sample_msg": "ZeroDivisionError: x",
           "first_seen": T0, "count": 50, "last_user": "sam", "sample_request_id": "abc123abc123abcd"}
    assert g.alert_subject("x", grp, False) == "[Annex] new error in x: ZeroDivisionError in compute"
    assert "error is back in x" in g.alert_subject("x", grp, True)
    body = g.alert_body("x", grp)
    assert "in r7" in body and "50 times" in body and "annex logs x --request abc123abc123abcd" in body and "sam" in body
    assert g.summary_subject("x", 3) == "[Annex] x: 3 more kinds of error"
    assert "annex errors x" in g.summary_body("x", [grp])


def test_two_tracebacks_in_a_row_are_two_events_not_a_chain():
    one = [row("Traceback (most recent call last):", 0, "error"), row('  File "/a/x.py", line 1, in f', 1), row("ValueError: a", 2, "error")]
    two = [row("Traceback (most recent call last):", 100, "error", h="h2"), row('  File "/a/x.py", line 1, in f', 101, h="h2b"),
           row("ValueError: b", 102, "error", h="h2c")]
    ev, _ = g.assemble(one + two + [row("later", 9000)])
    assert [e.h for e in ev] == [one[0]["h"], "h2"] and ev[0].fingerprint == ev[1].fingerprint


def test_the_frame_is_the_apps_own_code_not_the_frameworks_wrapper():
    stack = ('Traceback (most recent call last):\n  File "/app/main.py", line 25, in request_log\n    response = await call_next(request)\n'
             '  File "/usr/local/lib/python3.13/site-packages/starlette/middleware/base.py", line 179, in call_next\n    raise app_exc\n'
             '  File "/usr/local/lib/python3.13/site-packages/starlette/_exception_handler.py", line 53, in wrapped_app\n    raise exc\n'
             '  File "/app/main.py", line 99, in index\n    raise RuntimeError("x")\nRuntimeError: x')
    assert g.last_frame(stack) == ("/app/main.py", "index")
    lib_only = '  File "/usr/local/lib/python3.13/site-packages/a.py", line 1, in f\n  File "/usr/local/lib/python3.13/site-packages/b.py", line 2, in h'
    assert g.last_frame(lib_only)[1] == "h"
    node = "Error: x\n    at handler (/app/server.js:10:5)\n    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)"
    assert g.last_frame(node) == ("/app/server.js", "handler")
    assert g.last_frame("no frames here") is None


def test_a_finished_trace_at_the_end_of_a_window_is_complete_once_a_chain_can_no_longer_follow():
    rows = [row("Traceback (most recent call last):", 0, "error"), row('  File "/a/x.py", line 1, in f', 1), row("ValueError: a", 2, "error")]
    end = rows[-1]["ts"].timestamp()
    ev, open_ = g.assemble(rows, window_end=end + 1)          # a chained trace could still follow
    assert ev == [] and len(open_) == 1
    ev, open_ = g.assemble(rows, window_end=end + g.TRACE_GAP_S + 0.1)
    assert len(ev) == 1 and open_ == {}
