import json
from types import SimpleNamespace

import pytest

from src.report import build_report


@pytest.mark.parametrize("setting,expected", [("", 10), ("5", 5), ("invalid", 10), ("0", 1)])
def test_report_serializes_timing_metrics_offline(tmp_path, monkeypatch, setting, expected):
    monkeypatch.setenv("REPLY_TIMING_MIN_SENDS", setting)
    rows = {"Leads": [{"email": "fictional@example.com", "sent_at": "2026-10-06T14:00:00Z",
                       "status": "hot", "replied_at": "2026-10-06T15:00:00Z"}]}
    sheets = SimpleNamespace(book=SimpleNamespace(worksheet=lambda tab: SimpleNamespace(
        get_all_records=lambda: rows.get(tab, [])
    )))
    html_path = build_report(sheets, out_dir=tmp_path)
    metrics = json.loads((tmp_path / "data.json").read_text())
    timing = metrics["reply_timing"]
    assert timing["min_sends"] == expected
    assert timing["totals"]["sent"] == 1
    assert timing["time_to_first_reply"]["median_seconds"] == 3600
    assert "example.com" not in json.dumps(timing)
    # The existing renderer automatically embeds the additive metrics block.
    assert json.dumps(timing, ensure_ascii=False) in html_path.read_text()
