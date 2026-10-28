import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from src import report_fixture
from src.metrics import reply_timing


def cell(timing, weekday, hour):
    return next(row for row in timing["heatmap"]["cells"]
                if (row["weekday"], row["hour"]) == (weekday, hour))


def test_fixture_is_reproducible_fresh_and_uses_only_example_addresses():
    rows = report_fixture.generate_fixture_rows()
    assert rows == report_fixture.generate_fixture_rows()
    assert rows != report_fixture.generate_fixture_rows(seed=17)
    assert 300 <= len(rows["leads"]) <= 1000
    emails = [row["email"] for row in rows["leads"]]
    assert len(set(emails)) == len(emails)
    assert all(email.endswith("@example.com") for email in emails)
    for tab in ("drafts", "replies"):
        assert all(row["lead_email"] in emails for row in rows[tab])
    assert {r["category"] for r in rows["replies"]} == {"human", "auto_reply", "bounce"}
    assert {r.get("classification") for r in rows["replies"] if r["category"] == "human"} == {
        "positive", "neutral", "negative", "",
    }
    rows["leads"][0]["email"] = "changed@example.com"
    assert report_fixture.generate_fixture_rows()["leads"][0]["email"] != "changed@example.com"


def test_fixture_exercises_chart_states_and_counts_reconcile():
    rows = report_fixture.generate_fixture_rows()
    original = deepcopy(rows)
    timing = reply_timing(**rows)
    assert rows == original
    totals = timing["totals"]
    assert totals["sent"] == sum(c["sent"] for c in timing["heatmap"]["cells"])
    for key in ("sent", "delivered", "replies", "bounced", "positive_replies"):
        assert totals[key] == sum(c[key] for c in timing["by_weekday"])
        assert totals[key] == sum(c[key] for c in timing["by_hour"])
    assert totals["sent"] == totals["delivered"] + totals["bounced"]
    assert totals["replies"] <= totals["delivered"]
    assert totals["sent"] + timing["excluded"]["follow_up_recipients"] + 2 == len(rows["leads"])
    assert timing["excluded"]["invalid_send_recipients"] == 1
    assert timing["excluded"]["unsent_recipients"] == 1
    assert timing["excluded"]["missing_reply_timestamps"] == 1
    assert timing["excluded"]["negative_reply_delays"] == 1
    assert cell(timing, 0, 9)["sent"] == 6
    assert cell(timing, 0, 9)["low_sample"]
    assert cell(timing, 0, 10)["reply_rate"] == 0
    assert cell(timing, 4, 15)["sent"] == 0
    assert cell(timing, 4, 15)["reply_rate"] is None
    assert cell(timing, 3, 15)["sent"] > 0
    assert cell(timing, 3, 15)["delivered"] == 0
    assert cell(timing, 0, 8)["positive_rate_available"]
    assert 6 in timing["heatmap"]["weekdays"]
    assert 23 in timing["heatmap"]["hours"]
    latency = timing["time_to_first_reply"]
    assert latency["n"] == totals["replies"] - 2
    assert sum(b["count"] for b in latency["histogram"]) == latency["n"]
    assert all(b["count"] > 0 for b in latency["histogram"])


def test_build_fixture_preview_is_offline_and_serializes_the_same_metrics(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A fixture preview must not access Sheets or open a browser")

    monkeypatch.setattr("src.report.collect", forbidden)
    monkeypatch.setattr("src.sheets.SheetClient.__init__", forbidden)
    monkeypatch.setattr(report_fixture.webbrowser, "open", forbidden)
    path = report_fixture.build_fixture_report(tmp_path, seed=17, min_sends=15)
    html = path.read_text()
    metrics = json.loads((tmp_path / "data.json").read_text())
    json.dumps(metrics, allow_nan=False)
    embedded = json.loads(re.search(r"const M = (.*);", html).group(1))
    assert embedded == metrics
    assert metrics["preview"] == {"synthetic": True, "seed": 17}
    assert metrics["reply_timing"]["min_sends"] == 15
    assert "SYNTHETIC PREVIEW · Fictional data only" in html
    assert "example.com" not in json.dumps(metrics)
    assert metrics["reply_timing"] == reply_timing(**report_fixture.generate_fixture_rows(17), min_sends=15)


def test_preview_default_uses_a_temporary_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(report_fixture, "mkdtemp", lambda **kwargs: str(tmp_path / "preview"))
    path = report_fixture.build_fixture_report()
    assert path == tmp_path / "preview" / "index.html"
    assert path.exists()


@pytest.mark.parametrize("suffix", ["", "nested-preview"])
def test_preview_cannot_overwrite_the_published_dashboard(suffix):
    published = Path(report_fixture.__file__).resolve().parent.parent / "dashboard"
    with pytest.raises(ValueError, match="outside dashboard"):
        report_fixture.build_fixture_report(published / suffix)


def test_preview_checks_symlink_destinations(tmp_path):
    published = Path(report_fixture.__file__).resolve().parent.parent / "dashboard"
    alias = tmp_path / "alias"
    alias.symlink_to(published, target_is_directory=True)
    with pytest.raises(ValueError, match="outside dashboard"):
        report_fixture.build_fixture_report(alias)


def test_preview_cli_options_and_open_use_only_the_generated_file(tmp_path, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(report_fixture.webbrowser, "open", opened.append)
    report_fixture.main(["--out-dir", str(tmp_path), "--seed", "17", "--min-sends", "15", "--open"])
    assert opened == [f"{(tmp_path / 'index.html').as_uri()}#timingHeading"]
    assert "SYNTHETIC PREVIEW (seed 17)" in capsys.readouterr().out
    assert json.loads((tmp_path / "data.json").read_text())["reply_timing"]["min_sends"] == 15


@pytest.mark.parametrize("threshold", ["0", "-1", "bad"])
def test_preview_cli_rejects_bad_threshold_before_writing(tmp_path, threshold):
    destination = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as exc:
        report_fixture.main(["--out-dir", str(destination), "--min-sends", threshold])
    assert exc.value.code == 2
    assert not destination.exists()
