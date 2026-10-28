"""Command scheduling guards must run before any live client is constructed."""
import logging
from unittest.mock import Mock

import pytest

from src import main, schedule
from src.schedule import CENTRAL
from datetime import datetime


class ReachedClients(Exception):
    """Stop an allowed run before it can access credentials or the network."""


@pytest.fixture
def guarded_clients(monkeypatch):
    monkeypatch.setattr(main, "now_ct", lambda: datetime(2026, 11, 26, 10, tzinfo=CENTRAL))
    monkeypatch.setattr(main, "org_address_configured", lambda: True)
    monkeypatch.setenv("SEND_WINDOW_START_HOUR", "8")
    monkeypatch.setenv("SEND_WINDOW_END_HOUR", "16")
    sheets = Mock(side_effect=ReachedClients)
    monkeypatch.setattr(main, "SheetClient", sheets)
    return sheets


@pytest.mark.parametrize("command", [main.cmd_prepare, main.cmd_send])
@pytest.mark.parametrize("day,reason", [
    (datetime(2026, 11, 26, 10, tzinfo=CENTRAL), "Configured blackout date"),
    (datetime(2026, 12, 21, 10, tzinfo=CENTRAL), "winter break"),
    (datetime(2027, 1, 4, 10, tzinfo=CENTRAL), "winter break"),
])
def test_blackout_exits_before_clients(command, day, reason, guarded_clients, monkeypatch, caplog):
    monkeypatch.setattr(main, "now_ct", lambda: day)
    with caplog.at_level(logging.INFO, logger="cube.main"):
        assert command(dry_run=False) == 0
    guarded_clients.assert_not_called()
    assert reason in caplog.text
    assert "--force" in caplog.text


@pytest.mark.parametrize("command,dry_run,force", [
    (main.cmd_prepare, False, True),
    (main.cmd_send, False, True),
    (main.cmd_send, True, False),
])
def test_force_and_dry_run_bypass_blackout(command, dry_run, force, guarded_clients):
    with pytest.raises(ReachedClients):
        command(dry_run=dry_run, force=force)
    guarded_clients.assert_called_once_with()


@pytest.mark.parametrize("command", [main.cmd_prepare, main.cmd_send])
def test_normal_day_reaches_clients(command, guarded_clients, monkeypatch):
    monkeypatch.setattr(main, "now_ct", lambda: datetime(2027, 1, 5, 10, tzinfo=CENTRAL))
    with pytest.raises(ReachedClients):
        command(dry_run=False)
    guarded_clients.assert_called_once_with()


@pytest.mark.parametrize("command", [main.cmd_prepare, main.cmd_send])
def test_bad_calendar_fails_before_clients(command, guarded_clients, tmp_path, monkeypatch):
    path = tmp_path / "blackout_dates.yaml"
    path.write_text("dates: [not-a-date]\n")
    monkeypatch.setattr(schedule, "BLACKOUT_PATH", path)
    with pytest.raises(ValueError, match=r"dates\[0\] must be a date"):
        command(dry_run=False)
    guarded_clients.assert_not_called()
