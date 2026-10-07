"""
Page provenance recovery.

The original parser flattened the PDF into one markdown string, so no chunk
knew which page it came from. For documents that were already ingested (and
we do not want to re-run the paid LLM enrichment for), we recover the page by
text overlap: each section is matched to the page whose text shares the most
word n-grams with it.

This is deliberately tolerant. Coreference resolution rewrote some pronouns,
and PDF text extraction differs slightly from markdown conversion, so exact
substring matching would miss. N-gram containment survives both.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set, Tuple

_WORD = re.compile(r"[a-z0-9]+")

# Below this containment score we refuse to guess a page.
MIN_CONTAINMENT = 0.12
# A unit spanning several pages: keep pages scoring at least this fraction of the best.
SPAN_RATIO = 0.55


def shingles(text: str, n: int = 3) -> Set[str]:
    words = _WORD.findall(text.lower())
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def build_page_index(page_texts: Sequence[str], n: int = 3) -> List[Set[str]]:
    return [shingles(text, n) for text in page_texts]


def locate_pages(
    unit_text: str, page_index: Sequence[Set[str]], n: int = 3
) -> Optional[Tuple[int, int]]:
    """Return 1-based (first_page, last_page) for a unit, or None if unsure."""
    target = shingles(unit_text, n)
    if not target or not page_index:
        return None

    scores = [len(target & page) / len(target) for page in page_index]
    best = max(scores)
    if best < MIN_CONTAINMENT:
        return None

    floor = max(MIN_CONTAINMENT, best * SPAN_RATIO)
    pages = [i + 1 for i, s in enumerate(scores) if s >= floor]
    return min(pages), max(pages)
