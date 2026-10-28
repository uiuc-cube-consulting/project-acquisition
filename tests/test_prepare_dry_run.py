"""Preparation previews must never construct live sourcing or storage clients."""
import re
import socket
import sys
from unittest.mock import Mock

import pytest

from src import draft, llm, main, sheets
from src.gmail_send import GmailSender
from src.models import Lead, PastProject, TemplateType
from src.past_projects import PastProjectIndex
from src.template import TemplateRouter


SERVICE_SETTINGS = (
    "APOLLO_API_KEY", "APOLLO2_API_KEY", "HUNTER_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE", "SHEET_ID",
    "ALUMNI_SHEET_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
)


@pytest.fixture(autouse=True)
def isolated_preview(monkeypatch):
    """Even configured credentials must not enable live sourcing in dry runs."""
    for key in SERVICE_SETTINGS:
        monkeypatch.setenv(key, "unused-preview-placeholder")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_PREPARE_TARGET", "15")
    monkeypatch.setenv("ALUMNI_TARGET_SHARE", "0.35")
    monkeypatch.setenv("ENTERPRISE_TARGET_SHARE", "0.35")
    monkeypatch.setenv("COMPANY_DEDUPE", "1")
    monkeypatch.setenv("AUTO_APPROVE", "1")
    monkeypatch.setenv("ENABLE_FOLLOW_UPS", "1")
    for key in ("ORG_PHYSICAL_ADDRESS", "ORG_NAME", "UNSUBSCRIBE_MAILTO"):
        monkeypatch.delenv(key, raising=False)

    guards = []
    for owner, attr in (
        (main, "SheetClient"), (main, "ApolloClient"), (main, "HunterClient"),
        (main, "fetch_alumni_leads"), (main, "search_fresh_candidates"),
        (main, "bulk_reveal"), (main, "enterprise_candidates"),
        (main, "prepare_follow_ups"), (main, "_refresh_dashboard"),
        (main, "now_ct"), (main, "send_daily_summary"),
        (GmailSender, "send"), (draft, "generate_json"), (llm, "generate_json"),
        (llm.genai, "Client"), (socket.socket, "connect"),
    ):
        guard = Mock(side_effect=AssertionError(f"Unexpected call: {attr}"))
        monkeypatch.setattr(owner, attr, guard)
        guards.append(guard)
    yield
    # Some production helpers catch exceptions: also fail on swallowed attempts.
    for guard in guards:
        guard.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "   "])
@pytest.mark.parametrize("company_dedupe", ["0", "1"])
def test_offline_prepare_never_touches_services(key, company_dedupe, monkeypatch, capsys):
    if key is not None:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    monkeypatch.setenv("COMPANY_DEDUPE", company_dedupe)
    assert main.cmd_prepare(dry_run=True) == 0
    output = capsys.readouterr().out
    assert "[OFFLINE PREVIEW]" in output
    assert output.index("Selection report:") < output.index("TO:")
    assert "quota=alumni" in output
    assert "quota=discovery (non-alumni)" in output
    assert "template=cube_member (business)" in output
    assert "template=hybrid_product" in output
    assert "template=technical_engineering" in output
    assert "template=technical_software" in output
    for lead in main._dry_run_fixture_leads():
        assert f"  {lead.name}: quota=" in output
        assert f"TO: {lead.email}" in output
    assert len(re.findall(r"score=\d+\.\d{2}; template=.*; projects=", output)) == 7
    assert "fellow CUBE alum" in output
    assert "{contact_name}" not in output
    assert "{credibility_line}" not in output


def test_prepare_with_no_credentials(monkeypatch, capsys):
    for key in SERVICE_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    assert main.cmd_prepare(dry_run=True) == 0
    output = capsys.readouterr().out
    assert "[OFFLINE PREVIEW]" in output
    assert output.count("TO:") == 7


def test_configured_key_uses_existing_gemini_path(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    # Replace the model boundary, keeping the test offline.
    gemini_path = Mock(side_effect=draft.draft_offline_for_leads)
    monkeypatch.setattr(main, "draft_for_leads", gemini_path)
    assert main.cmd_prepare(dry_run=True) == 0
    gemini_path.assert_called_once()
    leads, _, _, sender_name, _ = gemini_path.call_args.args
    assert len(leads) == 7
    assert sender_name == "Jane Doe"
    output = capsys.readouterr().out
    assert "Gemini preview" in output
    assert "[OFFLINE PREVIEW]" not in output
    assert "TO:" in output


def test_live_prepare_does_not_fall_back_to_offline_drafting(monkeypatch):
    """A missing Gemini key must not change production behavior."""
    for key in ("APOLLO_API_KEY", "APOLLO2_API_KEY", "HUNTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMPANY_DEDUPE", "0")
    monkeypatch.setenv("ORG_PHYSICAL_ADDRESS", "123 Main St")
    monkeypatch.setenv("SENDER_NAME", "Alex Example")
    monkeypatch.setenv("SENDER_PHONE", "202-555-0100")
    fake_sheet = Mock()
    fake_sheet.get_suppression_emails.return_value = set()
    fake_sheet.get_known_emails.return_value = set()
    fake_sheet.get_known_linkedins.return_value = set()
    fake_sheet.get_contacted_dates.return_value = {}
    fake_sheet.fetch_prospect_leads.return_value = main._dry_run_fixture_leads()
    fake_sheet.fetch_alumni_targets.return_value = ([], [])
    monkeypatch.setattr(main, "SheetClient", Mock(return_value=fake_sheet))
    monkeypatch.setattr(sheets, "load_service_account_info", lambda: {})
    monkeypatch.setattr(main, "fetch_alumni_leads", lambda _: [])
    live_drafter = Mock(side_effect=RuntimeError("production drafter reached"))
    monkeypatch.setattr(main, "draft_for_leads", live_drafter)
    with pytest.raises(RuntimeError, match="production drafter reached"):
        main.cmd_prepare(dry_run=False, force=True)
    fake_sheet.bootstrap.assert_called_once_with()
    live_drafter.assert_called_once()
    fake_sheet.append_leads.assert_not_called()


def test_cli_dry_run_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_dotenv", lambda: False)
    monkeypatch.setattr(sys, "argv", ["cube-outreach", "prepare", "--dry-run"])
    assert main.main() == 0
    assert "[OFFLINE PREVIEW]" in capsys.readouterr().out


def test_zero_target_exits_without_services(monkeypatch, capsys):
    monkeypatch.setenv("DAILY_PREPARE_TARGET", "0")
    assert main.cmd_prepare(dry_run=True) == 0
    assert "no fixture leads selected" in capsys.readouterr().out


def test_fixture_contacts_are_fictional_and_cover_templates():
    leads = main._dry_run_fixture_leads()
    assert all(lead.email.rsplit("@", 1)[1].endswith(".example.com")
               and not lead.linkedin for lead in leads)
    assert any(lead.is_uiuc_alum for lead in leads)
    assert any(not lead.is_uiuc_alum for lead in leads)
    assert any(lead.is_cube_member for lead in leads)
    router = TemplateRouter()
    assert {router.route(lead.industry) for lead in leads} == set(TemplateType)


@pytest.mark.parametrize("industry", ["Robotics", None])
@pytest.mark.parametrize("member", [False, True])
@pytest.mark.parametrize("has_match", [False, True])
def test_offline_templates_are_filled(industry, member, has_match, monkeypatch):
    monkeypatch.setattr(draft, "TARGET_TERM", "Spring 2099")
    lead = Lead(name="Jane Doe", company="Example Robotics", email="jane@example.com",
                industry=industry, is_cube_member=member)
    project = PastProject(semester="Fall 2098", client="Example Past Client",
                          keywords=["robotics"], deliverables="Prototype design")
    index = Mock()
    index.top_matches.return_value = [project] if has_match else []
    [(selected, preview)] = draft.draft_offline_for_leads(
        [lead], TemplateRouter(), index, "Alex Example", "202-555-0100",
    )
    assert selected is lead
    assert preview.body.startswith("Hi Jane,")
    assert "Spring 2099" in preview.subject and "Spring 2099" in preview.body
    assert "Example Robotics" in preview.subject and "Example Robotics" in preview.body
    assert (industry or "your industry") in preview.body
    assert "Alex Example" in preview.body and "202-555-0100" in preview.body
    assert "info packet" in preview.body and "unsubscribe" in preview.body
    assert not re.search(r"\{\w+\}", preview.body)
    assert ("Example Past Client" in preview.body) is has_match
    assert ("fellow CUBE alum" in preview.body) is member
    assert not preview.approved and not preview.sent_at


def test_offline_credibility_uses_top_match():
    lead = Lead(name="Jane Doe", company="Example Studio", email="jane@example.com",
                industry="Robotics")
    top = PastProject(semester="Fall 2098", client="Example Robotics Project",
                      keywords=["robotics"], deliverables="Robotics prototype")
    other = PastProject(semester="Fall 2097", client="Example Finance Project",
                        keywords=["finance"], deliverables="Finance analysis")
    index = PastProjectIndex([other, top])
    [(_, preview)] = draft.draft_offline_for_leads(
        [lead], TemplateRouter(), index, "Alex Example", "202-555-0100",
    )
    assert "project for Example Robotics Project." in preview.body
    assert "Example Finance Project" not in preview.body
