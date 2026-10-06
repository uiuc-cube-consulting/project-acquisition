"""Tests for src/schedule.py — the send window and Central-time "today"."""
from datetime import datetime

import pytest

from src.schedule import CENTRAL, in_send_window, is_today_ct


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
