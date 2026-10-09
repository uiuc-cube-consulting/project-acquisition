"""Campaign settings loaded from config/campaign.yaml.

Environment variables override the YAML values when set. Blank environment
variables count as unset because the helpers in src.env handle that case.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .env import env_float, env_str


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "campaign.yaml"


def _load_campaign() -> dict:
    """Load the campaign configuration from YAML."""
    return yaml.safe_load(CONFIG_PATH.read_text())


def target_term() -> str:
    cfg = _load_campaign()
    return env_str("TARGET_TERM", str(cfg["target_term"]))


def campaign_start() -> str:
    cfg = _load_campaign()
    return env_str("CAMPAIGN_START", str(cfg["campaign_start"]))


def alumni_target_share() -> float:
    cfg = _load_campaign()
    default = float(cfg["alumni_target_share"])
    return min(1.0, max(0.0, env_float("ALUMNI_TARGET_SHARE", default)))


def enterprise_target_share() -> float:
    cfg = _load_campaign()
    default = float(cfg["enterprise_target_share"])
    return min(1.0, max(0.0, env_float("ENTERPRISE_TARGET_SHARE", default)))