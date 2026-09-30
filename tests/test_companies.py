"""Tests for src/companies.py — normalize_company, email_domain, CompanyRegistry.

Covers the cases from the issue #9 bug table, the README's "One company, one
conversation" examples, free-provider filtering, and registry claim/seen logic.
"""
import pytest

from src.companies import CompanyRegistry, email_domain, normalize_company


class TestNormalizeCompany:
    # --- issue #9 bug table ---

    def test_mckinsey_and_company_matches_mckinsey(self):
        assert normalize_company("McKinsey & Company") == normalize_company("McKinsey")

    def test_bain_and_company_matches_bain(self):
        assert normalize_company("Bain & Company") == normalize_company("Bain")

    def test_boston_consulting_group_does_not_match_boston_partners(self):
        assert normalize_company("Boston Consulting Group") != normalize_company("Boston Partners")

    def test_huron_consulting_group_matches_huron(self):
        assert normalize_company("Huron Consulting Group") == normalize_company("Huron")

    # --- README "One company, one conversation" examples ---

    def test_rsm_variants_match(self):
        assert normalize_company("RSM US LLP") == normalize_company("RSM")

    def test_deloitte_consulting_matches_deloitte(self):
        assert normalize_company("Deloitte Consulting LLP") == normalize_company("Deloitte")

    # --- existing doctests (regression) ---

    def test_the_boeing_company_matches_boeing(self):
        assert normalize_company("The Boeing Company") == normalize_company("Boeing")

    # --- edge cases ---

    def test_empty_string_returns_empty(self):
        assert normalize_company("") == ""

    def test_none_returns_empty(self):
        assert normalize_company(None) == ""

    def test_the_prefix_stripped(self):
        assert normalize_company("The Carlyle Group") == normalize_company("Carlyle Group")

    def test_apex_systems_and_apex_capital_stay_distinct(self):
        assert normalize_company("Apex Systems") != normalize_company("Apex Capital")

    def test_dangling_and_does_not_appear_in_key(self):
        key = normalize_company("McKinsey & Company")
        assert "and" not in key.split()


class TestEmailDomain:
    @pytest.mark.parametrize("email", [
        "user@gmail.com",
        "user@googlemail.com",
        "user@yahoo.com",
        "user@outlook.com",
        "user@hotmail.com",
        "user@icloud.com",
        "user@protonmail.com",
    ])
    def test_free_provider_returns_empty(self, email):
        assert email_domain(email) == ""

    def test_corporate_email_returns_domain(self):
        assert email_domain("partner@mckinsey.com") == "mckinsey.com"

    def test_mail_subdomain_stripped(self):
        assert email_domain("user@mail.acme.com") == "acme.com"

    def test_email_subdomain_stripped(self):
        assert email_domain("user@email.acme.com") == "acme.com"

    def test_none_returns_empty(self):
        assert email_domain(None) == ""

    def test_no_at_sign_returns_empty(self):
        assert email_domain("notanemail") == ""

    def test_empty_string_returns_empty(self):
        assert email_domain("") == ""


class TestCompanyRegistry:
    def test_seen_by_name(self):
        reg = CompanyRegistry(names={"mckinsey"})
        assert reg.seen(company="McKinsey") is True
        assert reg.seen(company="Deloitte") is False

    def test_seen_by_email_domain(self):
        reg = CompanyRegistry(domains={"mckinsey.com"})
        assert reg.seen(email="partner@mckinsey.com") is True
        assert reg.seen(email="someone@example.com") is False

    def test_free_provider_email_never_seen(self):
        reg = CompanyRegistry(domains={"gmail.com"})
        assert reg.seen(email="user@gmail.com") is False

    def test_claim_adds_name_and_domain(self):
        reg = CompanyRegistry()
        reg.claim(company="Acme Corp", email="contact@acme.com")
        assert reg.seen(company="Acme") is True
        assert reg.seen(email="other@acme.com") is True

    def test_claim_company_only(self):
        reg = CompanyRegistry()
        reg.claim(company="RSM US LLP")
        assert reg.seen(company="RSM") is True

    def test_empty_registry_sees_nothing(self):
        reg = CompanyRegistry()
        assert reg.seen(company="Anywhere Inc") is False
        assert reg.seen(email="user@example.com") is False

    def test_len_reflects_name_count(self):
        reg = CompanyRegistry(names={"rsm", "deloitte"})
        assert len(reg) == 2

    def test_from_rows_reads_leads_and_companies(self):
        leads = [
            {"company": "RSM US LLP", "email": "a@rsm.com"},
        ]
        companies = [
            {"company": "Deloitte", "domain": "deloitte.com"},
        ]
        reg = CompanyRegistry.from_rows(leads, companies)
        assert reg.seen(company="RSM") is True
        assert reg.seen(email="b@rsm.com") is True
        assert reg.seen(company="Deloitte") is True
        assert reg.seen(email="c@deloitte.com") is True
