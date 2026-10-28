from __future__ import annotations

import importlib

from src import campaign


def _write_config(tmp_path, **overrides):
    values = {
        "target_term": "Spring 2027",
        "campaign_start": "2026-08-18",
        "alumni_target_share": 0.35,
        "enterprise_target_share": 0.35,
    }
    values.update(overrides)

    path = tmp_path / "campaign.yaml"
    path.write_text(
        "\n".join(f"{key}: {value}" for key, value in values.items()) + "\n"
    )
    return path


def test_yaml_value_is_used(monkeypatch, tmp_path):
    path = _write_config(tmp_path, target_term="Fall 2027")
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.delenv("TARGET_TERM", raising=False)

    assert campaign.target_term() == "Fall 2027"


def test_env_var_overrides_yaml(monkeypatch, tmp_path):
    path = _write_config(tmp_path, target_term="Fall 2027")
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.setenv("TARGET_TERM", "Spring 2028")

    assert campaign.target_term() == "Spring 2028"


def test_blank_env_var_uses_yaml(monkeypatch, tmp_path):
    path = _write_config(tmp_path, target_term="Fall 2027")
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.setenv("TARGET_TERM", "")

    assert campaign.target_term() == "Fall 2027"


def test_bad_share_is_clamped(monkeypatch, tmp_path):
    path = _write_config(tmp_path, alumni_target_share=0.35)
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.setenv("ALUMNI_TARGET_SHARE", "1.5")

    assert campaign.alumni_target_share() == 1.0


def test_template_uses_term_from_yaml(monkeypatch, tmp_path):
    path = _write_config(tmp_path, target_term="Fall 2027")
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.delenv("TARGET_TERM", raising=False)

    import src.templates as templates

    try:
        importlib.reload(templates)
        assert templates.TARGET_TERM == "Fall 2027"
    finally:
        # Restore the real config so later tests see the module-level default.
        monkeypatch.undo()
        importlib.reload(templates)


def test_metrics_campaign_window_uses_yaml(monkeypatch, tmp_path):
    path = _write_config(tmp_path, target_term="Fall 2027", campaign_start="2027-01-05")
    monkeypatch.setattr(campaign, "CONFIG_PATH", path)
    monkeypatch.delenv("TARGET_TERM", raising=False)
    monkeypatch.delenv("CAMPAIGN_START", raising=False)

    from src import metrics

    assert metrics.campaign_window() == ("Fall 2027", "2027-01-05")
