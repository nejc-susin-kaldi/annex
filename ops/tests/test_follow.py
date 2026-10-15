from annex_ops import follow_logs as f

SUCCESS = ('\x1b[32m common.go:125 ▶ NOTICE\x1b[0m [Job "monthly-report-refresh" (3dc633c151b6)] '
           'Finished in "788.757231ms", failed: false, skipped: false, error: none')
FAILURE = ('\x1b[31m common.go:121 ▶ ERROR\x1b[0m [Job "alert-probe-check" (f4ee8fcf0adc)] '
           'Finished in "288.200879ms", failed: true, skipped: false, error: error non-zero exit code: 1')


def test_ofelia_success_and_failure_with_ansi_codes():
    ok = f.parse_ofelia(SUCCESS)
    assert ok == {"key": "monthly-report-refresh", "failed": False, "skipped": False, "error": None, "duration_ms": 788}
    bad = f.parse_ofelia(FAILURE)
    assert bad["failed"] is True and bad["key"] == "alert-probe-check" and bad["error"] == "error non-zero exit code: 1"


def test_ofelia_skipped_run_and_error_with_commas():
    skipped = f.parse_ofelia('[Job "a-b" (aa11)] Finished in "0s", failed: false, skipped: true, error: none')
    assert skipped["skipped"] is True
    err = f.parse_ofelia('[Job "a-b" (aa11)] Finished in "1s", failed: true, skipped: false, error: exit 2, then more, text')
    assert err["error"] == "exit 2, then more, text"


def test_ofelia_other_lines_are_ignored():
    for line in ('[Job "a-b" (aa11)] Started - python probe.py check', '[Job "a-b" (aa11)] StdOut: ok',
                 'New job registered "a-b" - "python x" - "0 * * * * *"', ""):
        assert f.parse_ofelia(line) is None


def test_go_durations():
    assert f.parse_duration_ms("788.757231ms") == 788
    assert f.parse_duration_ms("1.5s") == 1500
    assert f.parse_duration_ms("1m2.5s") == 62500
    assert f.parse_duration_ms("2h") == 7200000
    assert f.parse_duration_ms("500µs") == 0


def test_job_map_never_splits_on_dashes():
    containers = [
        {"Labels": {"com.kaldi.annex.app": "monthly-report",
                    "ofelia.job-exec.monthly-report-monthly.schedule": "0 0 6 1 * *",
                    "ofelia.job-exec.monthly-report-refresh.schedule": "0 */10 * * * *",
                    "ofelia.job-exec.monthly-report-refresh.command": "python report.py"}},
        {"Labels": {"com.kaldi.annex.app": "alert-probe", "ofelia.job-exec.alert-probe-check.schedule": "0 * * * * *"}},
        {"Labels": {"com.kaldi.annex.app": "x", "ofelia.job-exec.other-thing.schedule": "@daily"}},  # not this app's prefix
        {"Labels": {"ofelia.job-local.heartbeat.schedule": "@every 1h"}},
        {},
    ]
    assert f.job_map(containers) == {"monthly-report-monthly": ("monthly-report", "monthly"),
                                     "monthly-report-refresh": ("monthly-report", "refresh"),
                                     "alert-probe-check": ("alert-probe", "check")}


def test_traefik_access_log_entry_of_an_app_router():
    line = ('{"ClientAddr":"1.2.3.4","DownstreamStatus":500,"RouterName":"app-alert-probe@docker",'
            '"StartUTC":"2026-10-15T13:53:31.981769651Z","RequestPath":"/boom"}')
    app, ts, status = f.parse_traefik(line)
    assert (app, status) == ("alert-probe", 500) and abs(ts - 1792072411.98) < 0.01


def test_traefik_ignores_platform_routers_and_its_own_log_lines():
    assert f.parse_traefik('{"DownstreamStatus":200,"RouterName":"home@docker","StartUTC":"2026-10-15T13:53:31Z"}') is None
    assert f.parse_traefik('{"DownstreamStatus":404,"StartUTC":"2026-10-15T13:53:31Z"}') is None  # no router
    assert f.parse_traefik('time="2026-10-15" level=info msg="Configuration loaded"') is None
    assert f.parse_traefik("{not json") is None
    assert f.parse_traefik('{"RouterName":"app-x@docker","StartUTC":"2026-10-15T13:53:31Z"}') is None  # no status


def test_cursor_comparison_is_at_microsecond_precision():
    assert not f.newer(10.0000004, 10.0000001)  # the same microsecond as the stored cursor
    assert f.newer(10.000002, 10.000001)
