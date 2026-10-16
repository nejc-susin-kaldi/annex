"""The stored logs' partitions, retention, size guard and query parts (M2.2): pure functions only."""
from datetime import date, datetime, timezone

import pytest

from annex_ops import logstore as st
from annex_ops.errors import OpsError

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def test_partitions_for_today_and_two_days_ahead():
    assert st.partition_days(NOW) == [date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)]
    assert st.partition_days(datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc))[-1] == date(2027, 1, 2)


def test_a_line_belongs_to_its_utc_day():
    assert st.day_of(datetime(2026, 10, 16, 23, 59, 59, tzinfo=timezone.utc)) == date(2026, 10, 16)
    # 01:30 in Ljubljana (UTC+2) is still the 15th in UTC
    from zoneinfo import ZoneInfo
    assert st.day_of(datetime(2026, 10, 16, 1, 30, tzinfo=ZoneInfo("Europe/Ljubljana"))) == date(2026, 10, 15)


def test_partition_sql():
    assert st.partition_name(date(2026, 10, 16)) == "logs_20261016"
    sql = st.create_partition_sql(date(2026, 12, 31))
    assert "annex_ops.logs_20261231 PARTITION OF annex_ops.logs" in sql
    assert "FROM ('2026-12-31 00:00:00+00') TO ('2027-01-01 00:00:00+00')" in sql


def test_names_round_trip_and_foreign_names_are_ignored():
    assert st.partition_from_name("logs_20261016") == date(2026, 10, 16)
    assert st.partition_from_name("logs_dedupe") is None and st.partition_from_name("logs_2026") is None


def test_retention_drops_only_whole_partitions_older_than_the_window():
    days = [date(2026, 10, d) for d in range(1, 19)]
    expired = st.expired_days(days, NOW, 14)       # cutoff 2 Oct 10:00: a day is dropped once it has ended before that
    assert expired == [date(2026, 10, 1)]
    assert date(2026, 10, 2) not in expired        # it still holds lines from after 10:00
    assert st.expired_days(days, NOW, 1) == [date(2026, 10, d) for d in range(1, 15)]
    assert st.expired_days([], NOW, 14) == []


def test_the_size_guard_drops_oldest_first_and_never_the_newest():
    mb = 2**20
    sizes = {date(2026, 10, 10): 300 * mb, date(2026, 10, 11): 300 * mb, date(2026, 10, 12): 300 * mb, date(2026, 10, 13): 300 * mb}
    assert st.trim_plan(sizes, 2000 * mb) == []
    assert st.trim_plan(sizes, 700 * mb) == [date(2026, 10, 10), date(2026, 10, 11)]
    assert st.trim_plan(sizes, 1) == [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]   # the newest stays
    assert st.trim_plan({date(2026, 10, 13): 999 * mb}, 1) == []


def test_levels_include_the_ones_above():
    assert st.levels_from("warn") == ["warn", "error"] and st.levels_from("debug") == ["debug", "info", "warn", "error"]
    with pytest.raises(OpsError) as e:
        st.levels_from("fatal")
    assert "debug, info, warn or error" in e.value.message


def test_grep_text_is_literal():
    assert st.like_pattern("novak") == "%novak%"
    assert st.like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"
