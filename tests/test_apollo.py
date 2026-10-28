"""Tests for ApolloClient's key failover and email-lookup fallbacks.

No network: the client's session.post is replaced with a fake that answers by
(key, endpoint), so each test states exactly what each Apollo account returns.
"""
import pytest

from src.sourcing.apollo import (
    ApolloClient, ApolloRevealError, _to_lead, bulk_reveal, candidate_from_contact,
)

CREDITS = (422, '{"error":"insufficient credits"}')
FREE_PLAN = (403, '{"error":"not included in your Free plan","error_code":"API_INACCESSIBLE"}')


class FakeResponse:
    def __init__(self, status_code, text="", body=None):
        self.status_code = status_code
        self.text = text
        self._body = body or {}

    def json(self):
        return self._body


def make_client(monkeypatch, answers, keys=("k1", "k2")):
    """`answers` maps (key, endpoint) to a FakeResponse or a callable(kwargs)."""
    monkeypatch.setenv("APOLLO_API_KEY", keys[0])
    monkeypatch.setenv("APOLLO2_API_KEY", keys[1] if len(keys) > 1 else "")
    client = ApolloClient()
    client.calls = []

    def post(url, **kwargs):
        key = client.session.headers["X-Api-Key"]
        endpoint = url.rsplit("/v1/", 1)[1]
        client.calls.append((key, endpoint))
        answer = answers[(key, endpoint)]
        return answer(kwargs) if callable(answer) else answer

    client.session.post = post
    return client


def person(email):
    return {"name": "Pat Doe", "email": email, "organization": {"name": "Acme"}}


def match_by_name(kwargs):
    # people/match takes query-string params; echo an email built from them.
    p = kwargs["params"]
    return FakeResponse(200, body={"person": person(f"{p['first_name'].lower()}@acme.com")})


def cands(*names):
    return [candidate_from_contact(name=n, company="Acme") for n in names]


def test_switches_to_fallback_key_when_out_of_credits(monkeypatch):
    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(*CREDITS),
        ("k2", "people/bulk_match"): FakeResponse(200, body={"matches": [person("a@acme.com")]}),
    })
    assert client.bulk_match([{"id": "x"}]) == [person("a@acme.com")]
    assert client.key_name == "APOLLO2_API_KEY"


def test_free_plan_falls_back_to_single_match(monkeypatch):
    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(*CREDITS),
        ("k2", "people/bulk_match"): FakeResponse(*FREE_PLAN),
        ("k2", "people/match"): match_by_name,
    })
    leads = bulk_reveal(client, cands("Ann Lee", "Bo Kim"))
    assert [l.email for l in leads] == ["ann@acme.com", "bo@acme.com"]
    # Bulk is not retried on the Free key once it is known to be blocked.
    bulk_reveal(client, cands("Cy Ng"))
    assert client.calls.count(("k2", "people/bulk_match")) == 1


def test_single_match_no_result_is_none(monkeypatch):
    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(*FREE_PLAN),
        ("k1", "people/match"): FakeResponse(200, body={"person": None}),
    }, keys=("k1",))
    assert bulk_reveal(client, cands("Ann Lee")) == [None]


def test_failed_lookup_raises_with_partial_results(monkeypatch):
    seen = []

    def credits_after_first(kwargs):
        seen.append(kwargs)
        return match_by_name(kwargs) if len(seen) == 1 else FakeResponse(*CREDITS)

    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(*FREE_PLAN),
        ("k1", "people/match"): credits_after_first,
    }, keys=("k1",))
    with pytest.raises(ApolloRevealError) as info:
        bulk_reveal(client, cands("Ann Lee", "Bo Kim", "Cy Ng"))
    assert [l.email for l in info.value.done] == ["ann@acme.com"]


def test_bulk_failure_raises_instead_of_returning_no_match(monkeypatch):
    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(500, "server error"),
    }, keys=("k1",))
    with pytest.raises(ApolloRevealError):
        bulk_reveal(client, cands("Ann Lee"))


class FakeSheets:
    NOT_FOUND_MARKER = "NOT_FOUND"

    def __init__(self):
        self.written = {}

    def set_alumni_email(self, ref, value):
        self.written[ref] = value


def test_failed_lookup_does_not_mark_alumni_not_found(monkeypatch):
    from src.main import _Selector

    client = make_client(monkeypatch, {
        ("k1", "people/bulk_match"): FakeResponse(*CREDITS),
        ("k2", "people/bulk_match"): FakeResponse(*FREE_PLAN),
        ("k2", "people/match"): FakeResponse(*FREE_PLAN),
    })
    sheets = FakeSheets()
    queue = [candidate_from_contact(name=n, company=c, ref=i)
             for i, (n, c) in enumerate([("Ann Lee", "Acme"), ("Bo Kim", "Beta")])]
    selector = _Selector(apollo=client, sheets=sheets, scorer=None, known=set(),
                         suppression=set(), contacted=set(), budget=10)
    assert selector.take(queue, slots=2) == []
    assert sheets.written == {}
    assert selector.reveal_failed and selector.reveals == 0


def test_email_status_is_kept():
    lead = _to_lead({**person("ada@acme.com"), "email_status": "verified"}, source="test")
    assert lead.email_status == "verified"


def test_missing_email_status_is_none():
    lead = _to_lead(person("ada@acme.com"), source="test")
    assert lead.email_status is None


def test_locked_email_still_skipped():
    locked = {**person("email_not_unlocked@domain.com"), "email_status": "verified"}
    assert _to_lead(locked, source="test") is None
