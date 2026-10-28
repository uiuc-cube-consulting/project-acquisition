"""Tests for the Hunter fallback: credit pacing, Email Finder, Domain Search,
and how the selector hands Apollo's failures to Hunter.

No network: session.get is replaced with a fake that answers by endpoint.
"""
from datetime import date

from src.sourcing.apollo import candidate_from_contact
from src.sourcing.hunter import HunterClient, _linkedin_handle, _run_allowance

ACCOUNT = {"data": {"plan_name": "Free", "reset_date": "2099-01-01",
                    "requests": {"credits": {"used": 0, "available": 50, "remaining": 50}}}}


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def make_hunter(answers, max_credits=15):
    """`answers` maps an endpoint to a FakeResponse or a callable(params)."""
    client = HunterClient(api_key="h", max_credits=max_credits)
    client.calls = []
    answers = {"account": FakeResponse(200, ACCOUNT), **answers}  # caller may override

    def get(url, params=None, **kwargs):
        endpoint = url.rsplit("/v2/", 1)[1]
        client.calls.append((endpoint, params))
        answer = answers[endpoint]
        return answer(params) if callable(answer) else answer

    client.session.get = get
    return client


def found(email, score=95, status="valid"):
    return FakeResponse(200, {"data": {"email": email, "score": score,
                                       "verification": {"status": status}}})


class TestRunAllowance:
    def test_spreads_credits_over_weekdays_left(self):
        # Mon 2026-10-12 to reset Mon 2026-10-26: 10 weekdays, 50 credits.
        assert _run_allowance(50, date(2026, 10, 26), date(2026, 10, 12), cap=40) == 5

    def test_capped_per_run(self):
        assert _run_allowance(2000, date(2026, 10, 26), date(2026, 10, 12), cap=15) == 15

    def test_nothing_left(self):
        assert _run_allowance(0.5, date(2026, 10, 26), date(2026, 10, 12), cap=15) == 0


def test_linkedin_handle():
    assert _linkedin_handle("https://www.linkedin.com/in/pat-doe-123/?x=1") == "pat-doe-123"
    assert _linkedin_handle("https://www.linkedin.com/company/acme") is None


class TestFindEmail:
    def test_prefers_linkedin_handle(self):
        h = make_hunter({"email-finder": found("pat@acme.com")})
        assert h.find_email("Pat", "Doe", "Acme", "https://linkedin.com/in/patdoe") == "pat@acme.com"
        assert h.calls[-1] == ("email-finder", {"linkedin_handle": "patdoe"})

    def test_low_confidence_rejected_but_charged(self):
        h = make_hunter({"email-finder": found("pat@acme.com", score=40, status="unknown")})
        assert h.find_email("Pat", "Doe", "Acme") is None
        assert h.credits_used == 1

    def test_not_found_is_free(self):
        h = make_hunter({"email-finder": FakeResponse(404)})
        assert h.find_email("Pat", "Doe", "Acme") is None
        assert h.credits_used == 0

    def test_usage_limit_disables_hunter_for_the_run(self):
        h = make_hunter({"email-finder": FakeResponse(429, text="usage limit")})
        assert h.find_email("Pat", "Doe", "Acme") is None
        assert h.disabled
        h.find_email("Bo", "Kim", "Beta")
        assert sum(1 for e, _ in h.calls if e == "email-finder") == 1

    def test_stops_at_run_allowance(self):
        big = {"data": {**ACCOUNT["data"], "requests": {"credits": {"remaining": 10**6}}}}
        h = make_hunter({"account": FakeResponse(200, big), "email-finder": found("x@acme.com")},
                        max_credits=2)
        results = [h.find_email("Pat", "Doe", "Acme") for _ in range(4)]
        assert results == ["x@acme.com", "x@acme.com", None, None]


CONFIG = {
    "titles": ["Strategy"],
    "companies": [
        {"name": "Acme", "domain": "acme.com"},
        {"name": "Beta", "domain": "beta.com"},
        {"name": "Gamma", "domain": "gamma.com"},
    ],
}


def person(first, position, email, seniority="senior", confidence=95):
    return {"first_name": first, "last_name": "Doe", "position": position, "value": email,
            "seniority": seniority, "confidence": confidence,
            "verification": {"status": "valid"}}


def domain_answer(params):
    people = {
        "acme.com": [person("Ops", "Operations Manager", "ops@acme.com", "executive"),
                     person("Strat", "VP Strategy", "strat@acme.com")],
        "beta.com": [],
        "gamma.com": [person("Gil", "Head of Product", "gil@gamma.com")],
    }[params["domain"]]
    return FakeResponse(200, {"data": {"emails": people}})


def test_enterprise_leads_prefers_target_titles_and_stops_at_need():
    h = make_hunter({"domain-search": domain_answer})
    leads = h.enterprise_leads(CONFIG, registry=None, day_index=0, need=1)
    assert [l.email for l in leads] == ["strat@acme.com", "ops@acme.com"]
    assert all(l.company == "Acme" for l in leads)
    assert h.credits_used == 1


def test_enterprise_leads_empty_domain_is_free():
    h = make_hunter({"domain-search": domain_answer})
    leads = h.enterprise_leads(CONFIG, registry=None, day_index=1, need=1)  # starts at Beta
    assert [l.email for l in leads] == ["gil@gamma.com"]
    assert h.credits_used == 1


class FakeSheets:
    NOT_FOUND_MARKER = "NOT_FOUND"

    def __init__(self):
        self.written = {}

    def set_alumni_email(self, ref, value):
        self.written[ref] = value


class FakeScorer:
    def is_excluded(self, lead, suppression, contacted):
        return False, ""


def test_selector_uses_hunter_when_apollo_is_unavailable():
    from src.main import _Selector

    def finder(params):
        return found("ann@acme.com") if params.get("first_name") == "Ann" else FakeResponse(404)

    h = make_hunter({"email-finder": finder})
    sheets = FakeSheets()
    queue = [candidate_from_contact(name="Ann Lee", company="Acme", is_uiuc_alum=True, ref=1),
             candidate_from_contact(name="Bo Kim", company="Beta", is_uiuc_alum=True, ref=2)]
    selector = _Selector(apollo=None, sheets=sheets, scorer=FakeScorer(), known=set(),
                         suppression=set(), contacted=set(), budget=10, hunter=h)
    picked = selector.take(queue, slots=2)
    assert [l.email for l in picked] == ["ann@acme.com"]
    assert picked[0].is_uiuc_alum
    # Ann's email is cached; Bo's Hunter miss is NOT recorded as NOT_FOUND.
    assert sheets.written == {1: "ann@acme.com"}
