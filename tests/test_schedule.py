"""Tests for src/schedule.py — the send window and Central-time "today"."""
from datetime import date, datetime

import pytest

from src import schedule
from src.schedule import CENTRAL, blackout_reason, describe_send_window, in_send_window, is_blackout, is_today_ct


@pytest.fixture(autouse=True)
def default_window(monkeypatch):
    monkeypatch.delenv("SEND_WINDOW_START_HOUR", raising=False)
    monkeypatch.delenv("SEND_WINDOW_END_HOUR", raising=False)


@pytest.fixture
def blackout_config(tmp_path, monkeypatch):
    path = tmp_path / "blackout_dates.yaml"
    monkeypatch.setattr(schedule, "BLACKOUT_PATH", path)

    def write(content):
        path.write_text(content)
        return path

    return write


def ct(*args) -> datetime:
    return datetime(*args, tzinfo=CENTRAL)


class TestInSendWindow:
    @pytest.mark.parametrize("hour,expected", [(7, False), (8, True), (12, True), (15, True), (16, False)])
    def test_weekday_hours(self, hour, expected):
        assert in_send_window(ct(2026, 10, 6, hour, 30)) is expected  # a Tuesday

    def test_weekend_is_closed(self):
        assert in_send_window(ct(2026, 10, 10, 10, 0)) is False  # Saturday

    def test_utc_input_is_converted(self):
        # 20:30 UTC on Oct 6 is 3:30pm CDT — inside; 21:30 UTC is 4:30pm — outside.
        assert in_send_window(datetime.fromisoformat("2026-10-06T20:30:00+00:00"))
        assert not in_send_window(datetime.fromisoformat("2026-10-06T21:30:00+00:00"))

    def test_window_follows_dst(self):
        # 14:00 UTC is 9am CDT in October but 8am CST in December — both open;
        # 13:30 UTC is 8:30am CDT (open) but 7:30am CST (closed).
        assert in_send_window(datetime.fromisoformat("2026-10-06T13:30:00+00:00"))
        assert not in_send_window(datetime.fromisoformat("2026-12-08T13:30:00+00:00"))

    def test_window_is_configurable(self, monkeypatch):
        monkeypatch.setenv("SEND_WINDOW_START_HOUR", "10")
        assert not in_send_window(ct(2026, 10, 6, 9, 0))
        assert in_send_window(ct(2026, 10, 6, 10, 0))


class TestIsTodayCT:
    NOW = ct(2026, 10, 6, 9, 0)

    def test_sheet_utc_timestamp_same_central_day(self):
        assert is_today_ct("2026-10-06T14:00:00+00:00", self.NOW)

    def test_late_evening_utc_is_previous_central_day(self):
        # 02:00 UTC Oct 6 is 9pm CDT Oct 5.
        assert not is_today_ct("2026-10-06T02:00:00+00:00", self.NOW)

    @pytest.mark.parametrize("raw", ["", None, "not a date", "TRUE"])
    def test_blank_or_garbage(self, raw):
        assert not is_today_ct(raw, self.NOW)


class TestBlackouts:
    @pytest.mark.parametrize("day,blocked", [
        (date(2026, 11, 26), True),  # Thanksgiving
        (date(2026, 11, 27), True),  # Black Friday
        (date(2026, 12, 21), True),  # first day of winter break
        (date(2027, 1, 4), True),    # last day of winter break
        (date(2027, 1, 5), False),   # day after winter break
        (date(2027, 1, 18), True),   # MLK Day, mid spring campaign
        (date(2026, 10, 6), False),  # normal weekday
    ])
    def test_default_calendar(self, day, blocked):
        assert is_blackout(day) is blocked
        assert in_send_window(ct(day.year, day.month, day.day, 10)) is not blocked

    def test_reasons(self):
        assert blackout_reason(date(2026, 11, 26)) == "Configured blackout date"
        # A range's explanation takes precedence over overlapping single dates.
        assert blackout_reason(date(2026, 12, 25)) == "winter break"
        assert blackout_reason(date(2027, 1, 5)) is None

    def test_quoted_dates_and_range_without_reason(self, blackout_config):
        blackout_config('dates: ["2026-11-26"]\nranges:\n'
                        '  - {start: "2026-12-21", end: "2027-01-04"}\n')
        assert is_blackout(date(2026, 11, 26))
        assert blackout_reason(date(2027, 1, 4)) == "Configured blackout range"

    def test_utc_date_is_converted_before_blackout_check(self, blackout_config, monkeypatch):
        blackout_config("dates: [2026-11-26]\n")
        # Allow late hours to isolate the date check: UTC is already Thanksgiving,
        # but Central time is still Wednesday evening.
        monkeypatch.setenv("SEND_WINDOW_END_HOUR", "24")
        assert in_send_window(datetime.fromisoformat("2026-11-26T02:00:00+00:00"))
        assert not in_send_window(datetime.fromisoformat("2026-11-26T16:00:00+00:00"))

    @pytest.mark.parametrize("content,message", [
        ("dates: [not-a-date]", "dates[0] must be a date"),
        ('dates: ["2026-02-30"]', "dates[0] must be a date"),
        ("dates: [2026-02-30]", "cannot parse YAML"),
        ("dates: [true]", "dates[0] must be a date"),
        ("dates: [2026-11-26T10:00:00]", "dates[0] must be a date"),
        ("dates: 2026-11-26", "'dates' must be a list"),
        ("ranges: {}", "'ranges' must be a list"),
        ("ranges: [2026-12-21]", "ranges[0] must contain"),
        ("ranges: [{start: 2026-12-21}]", "ranges[0] must contain"),
        ("ranges: [{start: 2027-01-04, end: 2026-12-21}]", "start must be on or before end"),
        ("ranges: [{start: nope, end: 2027-01-04}]", "ranges[0].start must be a date"),
        ("ranges: [{start: 2026-12-21, end: 2027-01-04, why: ''}]", "why must be a non-empty string"),
        ("dates: []\nrangse: []", "containing only 'dates' and 'ranges'"),
        ("dates: [", "cannot parse YAML"),
        ("", "expected a mapping"),
    ])
    def test_malformed_config_fails_loudly(self, blackout_config, content, message):
        path = blackout_config(content)
        with pytest.raises(ValueError) as error:
            is_blackout(date(2026, 11, 26))
        assert str(path) in str(error.value)
        assert message in str(error.value)

    def test_later_malformed_entry_is_not_ignored(self, blackout_config):
        blackout_config("dates: [2026-11-26, not-a-date]\n")
        with pytest.raises(ValueError, match=r"dates\[1\]"):
            in_send_window(ct(2026, 11, 26, 10))


class TestDescribeSendWindow:
    @pytest.mark.parametrize("now,expected", [
        (ct(2026, 11, 25, 10), "next blackout: 2026-11-26"),
        (ct(2026, 11, 26, 10), "next blackout: 2026-11-26"),
        (ct(2026, 12, 22, 10), "next blackout: 2026-12-22 (winter break)"),
        (ct(2027, 1, 5, 10), "next blackout: 2027-01-18"),
    ])
    def test_next_blackout(self, now, expected):
        description = describe_send_window(now)
        assert "Mon-Fri 08:00-16:00 Central, excluding blackout dates" in description
        assert expected in description

    def test_calendar_runs_out(self, blackout_config):
        blackout_config("dates: [2026-11-26]\n")
        assert "no upcoming blackouts configured" in describe_send_window(ct(2026, 11, 27, 10))

    def test_next_blackout_does_not_depend_on_yaml_order(self, blackout_config):
        blackout_config("dates: [2026-11-27, 2026-11-26]\n")
        assert "next blackout: 2026-11-26" in describe_send_window(ct(2026, 11, 25, 10))
