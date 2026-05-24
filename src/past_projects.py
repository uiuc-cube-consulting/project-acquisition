"""Past-project loader + lightweight matcher.

For each lead's industry/keywords, find 1-2 past CUBE projects whose
keywords/deliverables overlap most. This gives the drafter a concrete
credibility line ("Our consultants recently helped X with Y") that
dramatically lifts cold-email reply rates.

The match is keyword-overlap + bag-of-words cosine on a small vocabulary,
which is plenty for 100 projects and avoids paying for an embedding call
on every lead.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Iterable

from .models import PastProject

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _vectorize(tokens: Iterable[str]) -> dict[str, int]:
    v: dict[str, int] = {}
    for t in tokens:
        v[t] = v.get(t, 0) + 1
    return v


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in a.keys() & b.keys())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


class PastProjectIndex:
    def __init__(self, projects: list[PastProject]) -> None:
        self.projects = projects
        self._vecs = [
            _vectorize(_tokenize(" ".join(p.keywords) + " " + p.deliverables))
            for p in projects
        ]

    @classmethod
    def load(cls, path: str | Path = "data/past_projects.json") -> "PastProjectIndex":
        data = json.loads(Path(path).read_text())
        return cls([PastProject(**row) for row in data])

    def top_matches(self, query: str, k: int = 2) -> list[PastProject]:
        qv = _vectorize(_tokenize(query))
        scored = [(self._cosine_with_keyword_boost(qv, vec, p), p)
                  for vec, p in zip(self._vecs, self.projects)]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for s, p in scored[:k] if s > 0]

    @staticmethod
    def _cosine_with_keyword_boost(
        qv: dict[str, int], pv: dict[str, int], project: PastProject
    ) -> float:
        base = _cosine(qv, pv)
        # Bonus if any query token matches a declared keyword (high-signal)
        kw_tokens = set()
        for kw in project.keywords:
            kw_tokens.update(_tokenize(kw))
        boost = 0.15 * len(qv.keys() & kw_tokens) / max(len(kw_tokens), 1)
        return base + boost
