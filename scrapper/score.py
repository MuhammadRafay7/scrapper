"""Weighted keyword scoring — decides whether a page/domain is actually in the niche."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import NicheConfig
from .extract import Page


@dataclass
class Score:
    value: float
    hits: dict[str, int]

    def top(self, n: int = 5) -> str:
        best = sorted(self.hits.items(), key=lambda kv: -kv[1])[:n]
        return ", ".join(f"{k}x{v}" for k, v in best)


@lru_cache(maxsize=4096)
def _pattern(phrase: str) -> re.Pattern:
    # Whole-word match, tolerant of hyphen/space variation inside a phrase.
    escaped = r"[\s\-_]+".join(re.escape(w) for w in phrase.lower().split())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.I)


def score_text(text: str, niche: NicheConfig, *, cap: int = 3) -> Score:
    """Each keyword counts up to `cap` times, so one stuffed page can't dominate."""
    haystack = re.sub(r"\s+", " ", text.lower())
    total = 0.0
    hits: dict[str, int] = {}
    for phrases, weight in niche.weights:
        for phrase in phrases:
            n = len(_pattern(phrase).findall(haystack))
            if n:
                counted = min(n, cap)
                hits[phrase] = n
                total += weight * counted
    return Score(round(total, 2), hits)


def score_page(page: Page, niche: NicheConfig) -> Score:
    """Title and URL are worth more than body text — they describe the whole page."""
    body = score_text(page.text, niche)
    title = score_text(f"{page.title} {page.url.replace('/', ' ').replace('-', ' ')}", niche, cap=1)
    return Score(round(body.value + title.value * 1.5, 2), {**body.hits, **title.hits})


def is_blocked_domain(domain: str, blocked: list[str]) -> bool:
    d = domain.lower()
    return any(d == b or d.endswith("." + b) for b in blocked)


def is_blocked_email(email: str, patterns: list[str]) -> bool:
    e = email.lower()
    return any(p in e for p in patterns)
