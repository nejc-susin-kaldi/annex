from annex_ops.engine import (Destroy, Die, Engine, GatusState, Health, JobResult, Maintenance, Oom, ReconcileDone,
                              Request, Restore, Seen, Start, Stop, Tuning)

APP = "app:probe"
C = dict(cid="c1", name="annex-app-probe", subject=APP)


def kinds(actions):
    return [(a.kind, a.signal) if a.signal else a.kind for a in actions if a.kind != "forget"]


def run(engine, events):
    """events: list of (time, input or 'tick'); returns all actions in order."""
    out = []
    for t, inp in events:
        out += engine.tick(t) if inp == "tick" else engine.handle(t, inp)
    return out


def engine(**kw):
    return Engine(Tuning(**kw))


def test_stop_noticed_by_docker_and_gatus_is_one_incident():
    e = engine()
    out = run(e, [(0, Stop(**C)), (1, Die(exit_code=137, **C)), (61, "tick"),
                  (120, GatusState(APP, True, "connection refused")), (130, "tick")])
    assert kinds(out) == ["open", ("add_signal", "down")]
    assert out[0].signals.keys() == {"stopped"}
    out = run(e, [(200, Start(**C)), (210, GatusState(APP, False))])
    assert kinds(out) == [("clear_signal", "stopped"), ("clear_signal", "down"), "resolve"]
    assert out[-1].duration_s == 210 - 61 and not out[-1].quiet


def test_resolve_waits_until_every_signal_has_cleared():
    e = engine()
    run(e, [(0, Stop(**C)), (61, "tick"), (70, GatusState(APP, True))])
    out = run(e, [(100, Start(**C))])
    assert kinds(out) == [("clear_signal", "stopped")]  # Gatus still says down
    assert kinds(run(e, [(130, GatusState(APP, False))])) == [("clear_signal", "down"), "resolve"]


def test_a_deploy_length_stop_alerts_nobody():
    e = engine()
    out = run(e, [(0, Stop(**C)), (1, Die(exit_code=0, **C)), (30, "tick"), (45, Start(**C)), (90, "tick")])
    assert out == []


def test_just_past_the_grace_period_it_counts():
    e = engine()
    assert run(e, [(0, Stop(**C)), (59, "tick")]) == []
    assert kinds(run(e, [(60, "tick")])) == ["open"]


def test_extras_stopped_is_ignored_but_unhealthy_is_not():
    e = engine()
    mb = dict(cid="m1", name="annex-metabase-1", subject="platform:metabase", ignore_stopped=True)
    assert run(e, [(0, Stop(**mb)), (200, "tick")]) == []
    assert kinds(run(e, [(300, Health(healthy=False, **mb))])) == ["open"]


def test_platform_service_stop_opens_a_platform_incident():
    e = engine()
    gw = dict(cid="g1", name="annex-gateway-1", subject="platform:gateway")
    out = run(e, [(0, Stop(**gw)), (61, "tick")])
    assert out[0].subject == "platform:gateway"


# --- crash loop -----------------------------------------------------------------
def crash_events(times, code=1):
    return [(t, Die(exit_code=code, **C)) for t in times]


def test_crash_loop_needs_three_nonzero_exits_in_the_window():
    e = engine()
    assert run(e, crash_events([0, 100])) == []
    out = run(e, crash_events([200]))
    assert kinds(out) == ["open"] and "crash-loop" in out[0].signals


def test_exits_outside_the_window_dont_count():
    e = engine()
    assert run(e, crash_events([0, 100, 700])) == []  # the first is 700 s old at the third (window 600)


def test_zero_exits_dont_count():
    e = engine()
    assert run(e, crash_events([0, 1, 2, 3], code=0)) == []


def test_crash_loop_clears_after_the_quiet_period_while_running():
    e = engine()
    run(e, crash_events([0, 10, 20]) + [(21, Start(**C))])
    assert run(e, [(300, "tick")]) == []                                # 280 s since the last exit: not yet
    assert kinds(run(e, [(620, "tick")])) == [("clear_signal", "crash-loop"), "resolve"]


def test_crash_loop_does_not_clear_while_the_container_is_down():
    e = engine()
    run(e, crash_events([0, 10, 20]))
    out = run(e, [(1000, "tick")])
    assert ("clear_signal", "crash-loop") not in kinds(out)


def test_oom_clears_with_the_crash_loop_rule():
    e = engine()
    out = run(e, [(0, Oom(**C)), (1, Die(exit_code=137, **C)), (2, Start(**C))])
    assert kinds(out) == ["open"] and "oom" in out[0].signals
    assert run(e, [(100, "tick")]) == []
    assert kinds(run(e, [(700, "tick")])) == [("clear_signal", "oom"), "resolve"]


def test_unhealthy_and_healthy():
    e = engine()
    assert kinds(run(e, [(0, Health(healthy=False, **C))])) == ["open"]
    assert kinds(run(e, [(50, Health(healthy=True, **C))])) == [("clear_signal", "unhealthy"), "resolve"]


# --- 5xx ----------------------------------------------------------------------
def requests(start, n, status, step=1.0):
    return [(start + i * step, Request("probe", start + i * step, status)) for i in range(n)]


def test_error_spike_needs_both_the_ratio_and_the_minimum():
    e = engine(error_window_s=300)
    # 9 of 9 is 100 % but under the minimum of 10
    assert run(e, requests(0, 9, 500) + [(20, "tick")]) == []
    # 10 errors among 1000 requests is 1 %: enough errors, too small a share
    e = engine()
    assert run(e, requests(0, 990, 200, 0.1) + requests(0, 10, 500, 5) + [(100, "tick")]) == []
    # 10 of 100 is 10 %: both
    e = engine()
    out = run(e, requests(0, 90, 200) + requests(0, 10, 500) + [(100, "tick")])
    assert kinds(out) == ["open"] and "errors" in out[0].signals


def test_error_spike_clears_after_a_full_window_below_the_threshold():
    e = engine(error_window_s=60)
    out = run(e, requests(0, 20, 500) + [(30, "tick")])
    assert kinds(out) == ["open"]
    # the burst ages out of the window at t=80; the clock for "below" starts at the first tick after that
    assert run(e, [(85, "tick")]) == []
    assert run(e, [(120, "tick")]) == []
    assert kinds(run(e, [(146, "tick")])) == [("clear_signal", "errors"), "resolve"]


def test_a_new_burst_resets_the_below_clock():
    e = engine(error_window_s=60)
    run(e, requests(0, 20, 500) + [(30, "tick"), (85, "tick")])
    run(e, requests(100, 20, 500) + [(120, "tick")])
    assert run(e, [(150, "tick")]) == []  # still over, so nothing clears


def test_4xx_dont_count():
    e = engine()
    out = run(e, requests(0, 50, 404) + [(60, "tick")])
    assert out == []


# --- jobs -----------------------------------------------------------------------
def test_job_failure_resolves_on_the_next_good_run_of_the_same_job_only():
    e = engine()
    out = run(e, [(0, JobResult("probe", "check", False, "exit 1"))])
    assert kinds(out) == ["open"] and out[0].subject == "job:probe/check"
    assert run(e, [(10, JobResult("probe", "other", True))]) == []   # another job's good run
    assert run(e, [(20, JobResult("other-app", "check", True))]) == []
    assert run(e, [(30, JobResult("probe", "check", False, "exit 1"))]) == []  # still failing: nothing new
    assert kinds(run(e, [(60, JobResult("probe", "check", True))])) == [("clear_signal", "job-failed"), "resolve"]


def test_job_incident_is_separate_from_the_app_incident():
    e = engine()
    run(e, [(0, JobResult("probe", "check", False))])
    out = run(e, [(1, Health(healthy=False, **C))])
    assert out[0].kind == "open" and out[0].subject == APP


# --- reminders ---------------------------------------------------------------------
def test_reminders_after_an_hour_then_daily():
    e = engine()
    run(e, [(0, Start(**C)), (1, GatusState(APP, True))])
    assert run(e, [(3600, "tick")]) == []
    assert kinds(run(e, [(3601, "tick")])) == ["remind"]
    assert run(e, [(3602, "tick"), (3700 + 80000, "tick")]) == []
    assert kinds(run(e, [(3601 + 86400, "tick")])) == ["remind"]
    assert kinds(run(e, [(3601 + 86400 + 10, "tick")])) == []


def test_reminder_lists_the_current_signals_and_the_age():
    e = engine()
    run(e, [(0, Start(**C)), (0, GatusState(APP, True, "x")), (10, Health(healthy=False, **C))])
    out = run(e, [(3601, "tick")])
    assert out[0].signals.keys() == {"down", "unhealthy"} and out[0].duration_s == 3601


# --- maintenance ---------------------------------------------------------------------
def test_maintenance_collects_signals_then_opens_when_it_ends():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 900))])
    assert run(e, [(10, Stop(**C)), (80, "tick"), (500, "tick")]) == []
    out = run(e, [(901, "tick")])
    assert kinds(out) == ["open"] and "stopped" in out[0].signals


def test_signals_that_cleared_during_maintenance_never_alert():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 900)), (10, Stop(**C)), (80, "tick"), (100, Start(**C))])
    assert run(e, [(901, "tick")]) == []


def test_ending_maintenance_early_releases_active_signals():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 9000)), (10, Stop(**C)), (80, "tick")])
    out = run(e, [(100, Maintenance(APP, None))])
    assert kinds(out) == ["open"]


def test_app_maintenance_covers_its_jobs():
    e = engine()
    run(e, [(0, Maintenance(APP, 900))])
    assert run(e, [(5, JobResult("probe", "check", False))]) == []


def test_reminders_are_held_during_maintenance():
    e = engine()
    run(e, [(0, Start(**C)), (1, GatusState(APP, True)), (3000, Maintenance(APP, 9000))])
    assert run(e, [(3700, "tick")]) == []


# --- destroy, recreate, reconcile ----------------------------------------------------------
def test_destroy_closes_the_incident_without_a_message():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (62, "tick"), (70, GatusState(APP, True))])
    out = run(e, [(100, Destroy("c1", APP))])
    assert [a.kind for a in out if a.kind != "forget"] == ["clear_signal", "clear_signal", "resolve"]
    assert out[-1].quiet
    assert any(a.kind == "forget" for a in out)
    assert e.open == {} and e.signals == {}


def test_destroy_also_closes_the_apps_job_incidents():
    e = engine()
    run(e, [(0, Start(**C)), (1, JobResult("probe", "check", False))])
    out = run(e, [(5, Destroy("c1", APP))])
    assert out[-1].kind == "resolve" and out[-1].quiet and out[-1].subject == "job:probe/check"


def test_gatus_for_an_app_with_no_container_is_ignored():
    e = engine()
    assert run(e, [(0, GatusState("app:ghost", True))]) == []


def test_platform_front_door_needs_no_container():
    e = engine()
    assert kinds(run(e, [(0, GatusState("platform:front-door", True))])) == ["open"]


def test_compose_recreate_does_not_alert():
    e = engine()
    new = dict(C, cid="c2")
    out = run(e, [(0, Start(**C)), (10, Stop(**C)), (10, Die(exit_code=137, **C)), (11, Start(**new)),
                  (12, Destroy("c1", APP)), (100, "tick")])
    assert [a for a in out if a.kind != "forget"] == []


def test_recreate_of_a_stopped_app_resolves_it_loudly():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (62, "tick")])
    new = dict(C, cid="c2")
    out = run(e, [(100, Start(**new)), (101, Destroy("c1", APP))])
    assert out[-1].kind == "resolve" and not out[-1].quiet


def test_reconcile_clears_a_stopped_signal_that_ended_while_ops_was_down():
    e = engine()
    run(e, [(0, Restore([{"subject": APP, "opened_at": 0, "last_notified_at": 0, "reminders": 0,
                          "signals": {"stopped": {"since": 0, "detail": "x", "cid": None}}}]))])
    out = run(e, [(100, Seen(running=True, health=None, **C)), (101, ReconcileDone({"c1"}))])
    assert kinds(out) == [("clear_signal", "stopped"), "resolve"]


def test_reconcile_forgets_containers_that_vanished_while_ops_was_down():
    e = engine()
    run(e, [(0, Start(**C))])
    out = run(e, [(50, ReconcileDone(set()))])
    assert any(a.kind == "forget" for a in out) and e.ctrs == {}


def test_reconcile_of_a_stopped_container_starts_the_grace_period():
    e = engine()
    run(e, [(0, Seen(running=False, **C))])
    assert run(e, [(30, "tick")]) == []
    assert kinds(run(e, [(61, "tick")])) == ["open"]


def test_restored_incident_keeps_reminding_from_its_original_time():
    e = engine()
    run(e, [(0, Restore([{"subject": APP, "opened_at": 1000, "last_notified_at": 1000, "reminders": 0,
                          "signals": {"down": {"since": 1000, "detail": "x", "cid": None}}}]))])
    assert kinds(run(e, [(1000 + 3601, "tick")])) == ["remind"]


def test_an_errors_signal_restored_after_an_ops_restart_still_clears():
    e = engine(error_window_s=60)
    run(e, [(0, Restore([{"subject": APP, "opened_at": 0, "last_notified_at": 0, "reminders": 0,
                          "signals": {"errors": {"since": 0, "detail": "x", "cid": None}}}]))])
    assert run(e, [(10, "tick")]) == []               # no requests in memory: the quiet period starts now
    assert kinds(run(e, [(71, "tick")])) == [("clear_signal", "errors"), "resolve"]


def test_a_job_failing_because_its_app_is_down_adds_no_second_alert():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C))])
    # the container is stopped but the grace period isn't over: the job's failure is a consequence
    assert run(e, [(5, JobResult("probe", "check", False, "container not running"))]) == []
    run(e, [(62, "tick")])  # now the app incident is open
    assert run(e, [(65, JobResult("probe", "check", False, "container not running"))]) == []
    out = run(e, [(100, Start(**C))])
    assert kinds(out) == [("clear_signal", "stopped"), "resolve"]
    assert e.signals == {}


def test_a_job_that_still_fails_after_the_app_is_back_alerts_on_its_own():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (5, JobResult("probe", "check", False)), (100, Start(**C))])
    out = run(e, [(160, JobResult("probe", "check", False, "exit 1"))])
    assert kinds(out) == ["open"] and out[0].subject == "job:probe/check"
