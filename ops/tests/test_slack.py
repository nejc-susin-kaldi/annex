import pytest

from annex_ops import slack
from annex_ops.errors import OpsError


def error_of(fn, *a, **kw) -> OpsError:
    with pytest.raises(OpsError) as e:
        fn(*a, **kw)
    return e.value


def test_channel_is_lower_cased_and_must_have_a_hash():
    assert slack.parse_channel({"channel": " #Finance-Reports ", "text": "x"}) == "#finance-reports"
    for bad in ("finance", "#", "# a", "#a b", "", None, 5):
        assert error_of(slack.parse_channel, {"channel": bad, "text": "x"}).status == 400


def test_only_granted_channels_pass():
    slack.check_channel("#finance-reports", ["#finance-reports"])
    slack.check_channel("#finance-reports", ["#Finance-Reports"])
    e = error_of(slack.check_channel, "#other", ["#finance-reports"])
    assert e.status == 403 and "#finance-reports" in e.message and "notify.slack" in e.message


def test_an_app_without_channels_is_refused_everywhere():
    e = error_of(slack.check_channel, "#a", [])
    assert e.status == 403 and "no channels" in e.message


def test_internal_callers_skip_the_grant():
    slack.check_channel("#annex-alerts", None)


def test_unknown_fields():
    e = error_of(slack.parse_channel, {"channel": "#a", "text": "x", "thread": 1})
    assert e.status == 400 and "channel, text, blocks" in e.message


def test_text_or_blocks_required_and_text_with_blocks():
    assert slack.parse_message({"text": "hi"}) == ("hi", None)
    assert slack.parse_message({"text": "hi", "blocks": [{"type": "divider"}]}) == ("hi", [{"type": "divider"}])
    assert "text is required whenever blocks" in error_of(slack.parse_message, {"blocks": [{"type": "divider"}]}).message
    assert error_of(slack.parse_message, {}).status == 400
    assert error_of(slack.parse_message, {"text": "hi", "blocks": "x"}).status == 400
    assert error_of(slack.parse_message, {"text": " "}).status == 400


def test_test_mode_sends_every_post_to_the_test_channel_marked_and_says_where_it_was_meant_for():
    from annex_ops import slack
    ch, text, note = slack.redirect("#finance-reports", "Report ready", True, "#annex-selftest")
    assert ch == "#annex-selftest" and text.startswith("[TEST] Report ready") and "#finance-reports" in text
    assert note == "test mode: posted to #annex-selftest"
    assert slack.redirect("#finance-reports", "Report ready", False, "#annex-selftest") == ("#finance-reports", "Report ready", None)


def test_test_mode_ends_by_itself(tmp_path, monkeypatch):
    import time
    from annex_ops import config
    f = tmp_path / "test-mode"
    monkeypatch.setattr(config, "TEST_MODE_FILE", str(f))
    assert config.test_mode_until() is None                      # no file: off
    f.write_text(str(time.time() + 600))
    assert config.test_mode_until() is not None                  # in the future: on
    f.write_text(str(time.time() - 1))
    assert config.test_mode_until() is None                      # a forgotten file stops mattering
    f.write_text("nonsense")
    assert config.test_mode_until() is None
