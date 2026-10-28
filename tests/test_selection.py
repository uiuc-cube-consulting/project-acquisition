"""Offline regression coverage for quota selection; no Sheets, Apollo or SMTP."""
from unittest.mock import Mock

import pytest

from src import main
from src.models import Lead
from src.sourcing.apollo import Candidate


def lead(index, *, alumni=False, source="sheet"):
    return Lead(name=f"Test Person {index}", company=f"Company {index}",
                email=f"person{index}@example.com", is_uiuc_alum=alumni, source=source)


def candidate(index):
    return Candidate(person={}, source="test", enrich=False, name=f"Person {index}",
                     title=None, company=f"Company {index}", industry=None,
                     location=None, company_stage=None, linkedin=None,
                     apollo_id=str(index), is_uiuc_alum=False)


@pytest.fixture
def selector(monkeypatch):
    scorer = Mock()
    scorer.is_excluded.return_value = (False, "")
    sheets = Mock()
    sheets.NOT_FOUND_MARKER = "NOT_FOUND"
    instance = main._Selector(apollo=object(), sheets=sheets, scorer=scorer,
                              known=set(), suppression=set(), contacted={},
                              budget=100)

    def reveal(_client, candidates):
        return [lead(int(c.apollo_id), source=c.source) for c in candidates]
    monkeypatch.setattr(main, "bulk_reveal", Mock(side_effect=reveal))
    return instance


def test_backfill_resumes_at_first_unconsumed_lead(selector):
    queue = [lead(i) for i in range(5)]
    assert selector.take(queue, 1) == queue[:1]
    assert selector.take(queue, 4) == queue[1:]
    assert main.bulk_reveal.call_count == 0


def test_known_email_leads_do_not_spend_or_require_reveal_budget(selector):
    selector.budget = 0
    queue = [candidate(0), lead(1), lead(2)]
    assert selector.take(queue, 2) == queue[1:]
    main.bulk_reveal.assert_not_called()


def test_budget_is_shared_across_queues_and_cached_reveals_are_reused(selector):
    selector.budget = 3
    queue = [candidate(i) for i in range(5)]
    assert len(selector.take(queue, 1)) == 1
    assert len(selector.take(queue, 4)) == 2
    assert selector.take([candidate(9)], 1) == []
    assert selector.reveals == 3
    assert sum(len(call.args[1]) for call in main.bulk_reveal.call_args_list) == 3


def test_suppressed_and_duplicate_addresses_never_selected(selector):
    selector.suppression = {"person1@example.com"}
    queue = [lead(1), lead(2), lead(2), lead(3)]
    assert [item.email for item in selector.take(queue, 4)] == [
        "person2@example.com", "person3@example.com",
    ]


def test_default_quotas_keep_non_alumni_slots(selector):
    alumni = [lead(i, alumni=True) for i in range(20)]
    enterprise = [lead(i) for i in range(30, 50)]
    discovery = [lead(i) for i in range(60, 80)]
    selected = main._select_quotas(selector, alumni, enterprise, discovery, 15, 5, 5)
    assert [len(group) for group in selected] == [5, 5, 5]
    assert len({item.email for group in selected for item in group}) == 15


@pytest.mark.parametrize("pool_sizes,expected", [
    ((20, 0, 0), (15, 0, 0)),
    ((0, 20, 0), (0, 15, 0)),
    ((0, 0, 20), (0, 0, 15)),
    ((2, 20, 1), (2, 12, 1)),
])
def test_thin_pools_backfill_without_losing_contactable_leads(selector, pool_sizes, expected):
    queues = [[lead(pool * 100 + i, alumni=pool == 0) for i in range(count)]
              for pool, count in enumerate(pool_sizes)]
    result = main._select_quotas(selector, *queues, 15, 5, 5)
    assert tuple(map(len, result)) == expected
    assert len({item.email for group in result for item in group}) == 15
