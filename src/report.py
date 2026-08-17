"""Standalone HTML dashboard.

Renders every metric in `metrics.py` into ONE self-contained file — no server,
no CDN, no network at open time — so it can be emailed as an attachment, dropped
in Slack, or published straight to GitHub Pages.

    python -m src.main report          # -> dashboard/index.html + dashboard/data.json
    python -m src.main report --open   # ...and open it

The page is a point-in-time snapshot: the metrics are baked into the file at
build time. Re-run the command to refresh it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .metrics import collect

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).with_name("report_template.html")
PLACEHOLDER = "/*__METRICS__*/{}"


def render_html(metrics: dict, template: str | None = None) -> str:
    """Inline the metrics into the template as a JSON literal."""
    html = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise ValueError(f"Template is missing the {PLACEHOLDER} placeholder")
    payload = json.dumps(metrics, ensure_ascii=False, default=str)
    # A literal "</script>" inside the JSON would close the tag early; "<!--"
    # would open an HTML comment. Neither can survive into the page.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    return html.replace(PLACEHOLDER, payload)


def build_report(sheets, out_dir: str | Path = "dashboard", daily_target: int = 15) -> Path:
    """Compute metrics and write `index.html` + `data.json` into `out_dir`."""
    metrics = collect(sheets, daily_target=daily_target)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    data_path = out / "data.json"
    data_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    html_path = out / "index.html"
    html_path.write_text(render_html(metrics), encoding="utf-8")

    log.info(
        "Report: %d sent, %s reply rate, %s delivered, %s Apollo find rate",
        metrics["headline"]["emails_sent"],
        _pct(metrics["reply"]["rate"]),
        _pct(metrics["deliverability"]["delivered_rate"]),
        _pct(metrics["apollo"]["find_rate"]),
    )
    return html_path


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"
