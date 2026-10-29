"""Reproducible, credential-free dashboard fixtures and a local HTML preview.

Run ``python -m src.report_fixture --open`` from the repository root. This
module never collects Sheet rows, loads .env, or invokes outreach commands.
The invented timing patterns demonstrate the charts, not sending advice.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from tempfile import mkdtemp
import webbrowser

from .metrics import compute
from .report import render_html
from .schedule import CENTRAL


def generate_fixture_rows(seed: int = 36) -> dict[str, list[dict]]:
    """Return fresh fake Leads, Drafts and Replies rows for a fixed seed.

    A few hundred recipients span four weeks. Deliberate empty, low-sample,
    zero-reply and bounce-only cells exercise distinct dashboard states.
    Extra rows cover DST, early replies and invalid/missing timestamps.
    Every recipient address is fictional and uses example.com.
    """
    rng = Random(seed)
    leads, drafts, replies = [], [], []

    def add(sent_at: str, *, status: str = "sent", replied_at: str = "") -> str:
        email = f"fixture-person-{len(leads):04d}@example.com"
        leads.append({"email": email, "sent_at": sent_at, "status": status,
                      "replied_at": replied_at, "source": "fixture"})
        drafts.append({"lead_email": email, "sent_at": sent_at,
                       "prepared_at": "2026-09-25T14:00:00Z",
                       "is_follow_up": False, "template_used": "business"})
        return email

    for day in range(5):
        for hour in range(8, 16):
            slot = (day, hour)
            count = rng.randint(10, 20)
            if slot == (4, 15):
                count = 0  # no data, distinct from sends with zero replies
            elif slot == (0, 9):
                count = 6  # visibly below the default threshold
            for j in range(count):
                sent = datetime(2026, 9, 28, hour, rng.randrange(60), tzinfo=CENTRAL)
                sent += timedelta(days=day + 7 * (j % 4))
                email = add(sent.isoformat())
                row = leads[-1]
                if slot == (3, 15) or j == 8:
                    row["status"] = "bounced"
                    replies.append({"lead_email": email, "category": "bounce"})
                elif j == 7:
                    replies.append({"lead_email": email, "category": "auto_reply"})
                elif slot != (0, 10) and j % 3 == 0:
                    # Tuesday mornings have more invented replies, deliberately.
                    delay = rng.choice([900, 7200, 28800, 86400, 172800, 345600])
                    received = sent.astimezone(timezone.utc) + timedelta(seconds=delay)
                    classification = ["positive", "neutral", "negative"][(j // 3) % 3]
                    if slot != (0, 8) and j == 6:
                        classification = ""  # incomplete sentiment coverage
                    row.update(status="hot" if classification == "positive" else "replied",
                               replied_at=received.isoformat())
                    replies.append({"lead_email": email, "category": "human",
                                    "received_at": received.isoformat(), "classification": classification})
                elif day == 1 and hour < 12 and j % 3 == 1:
                    received = sent.astimezone(timezone.utc) + timedelta(hours=2)
                    row.update(status="replied", replied_at=received.isoformat())
                    replies.append({"lead_email": email, "category": "human",
                                    "received_at": received.isoformat(), "classification": "neutral"})
                if j == 5 and slot != (0, 9):
                    follow_up = sent + timedelta(days=3)
                    row["last_follow_up_at"] = follow_up.isoformat()
                    drafts.append({**drafts[-1], "is_follow_up": True,
                                   "sent_at": follow_up.isoformat()})

    # Fixed cases keep edge conditions present for every seed, independent of RNG.
    for sent_at, replied_at in [
        ("2026-03-06T15:00:00Z", "2026-03-09T14:00:00Z"),  # spring DST: 71h
        ("2026-10-30T14:00:00Z", "2026-11-02T15:00:00Z"),  # autumn DST: 73h
        ("2026-10-06T14:00:00Z", "2026-10-07T11:00:00Z"),  # 6am Central reply
        ("2026-10-04T23:00:00-05:00", "2026-10-05T01:00:00-05:00"),
        ("2026-10-06T14:00:00Z", "2026-10-06T13:00:00Z"),  # negative delay
    ]:
        add(sent_at, status="replied", replied_at=replied_at)
    add("2026-10-06T14:00:00Z", status="replied")  # missing reply timestamp
    add("invalid timestamp")
    add("", status="drafted")
    return {"leads": leads, "drafts": drafts, "replies": replies}


def build_fixture_report(
    out_dir: str | Path | None = None, *, seed: int = 36, min_sends: int = 30,
) -> Path:
    """Write labeled synthetic HTML and aggregate JSON, outside dashboard/."""
    out = Path(out_dir) if out_dir is not None else Path(mkdtemp(prefix="reply-timing-preview-"))
    published = Path(__file__).resolve().parent.parent / "dashboard"
    if out.resolve().is_relative_to(published):
        raise ValueError("Synthetic previews must be written outside dashboard/")
    metrics = compute(**generate_fixture_rows(seed), alumni=[], reply_timing_min_sends=min_sends)
    metrics["preview"] = {"synthetic": True, "seed": seed}
    html = render_html(metrics)
    html = html.replace("<title>CUBE Outreach Metrics</title>",
                        "<title>SYNTHETIC PREVIEW · CUBE Outreach Metrics</title>")
    html = html.replace('<div class="wrap">', '<div class="wrap">\n'
                        '<p class="callout"><strong>SYNTHETIC PREVIEW · Fictional data only. '
                        'Patterns are invented for testing, not send-window recommendations.</strong></p>', 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path = out / "index.html"
    html_path.write_text(html, encoding="utf-8")
    return html_path.resolve()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="Output directory; defaults to a new temporary folder")
    parser.add_argument("--seed", type=int, default=36, help="Fixture seed (default: 36)")
    parser.add_argument("--min-sends", type=int, default=30, help="Small-sample threshold (default: 30)")
    parser.add_argument("--open", action="store_true", help="Open the preview in your default browser")
    args = parser.parse_args(argv)
    if args.min_sends < 1:
        parser.error("--min-sends must be a positive integer")
    try:
        path = build_fixture_report(args.out_dir, seed=args.seed, min_sends=args.min_sends)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"SYNTHETIC PREVIEW (seed {args.seed}): {path}")
    print(f"Open {path.as_uri()}#timingHeading")
    if args.open:
        webbrowser.open(f"{path.as_uri()}#timingHeading")


if __name__ == "__main__":
    main()
