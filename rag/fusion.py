"""Rank fusion and context budgeting - pure functions, no I/O."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Hashable, List, Mapping, Optional, Sequence, Tuple


def rrf_fuse(
    rankings: Mapping[str, Sequence[Hashable]],
    k: int = 60,
    weights: Optional[Mapping[str, float]] = None,
) -> List[Tuple[Hashable, float]]:
    """
    Reciprocal Rank Fusion.

    Combines ranked lists whose raw scores are not comparable (cosine
    similarity vs BM25-style rank vs MaxSim) by using only *rank*:
    score(d) = Σ weight_r / (k + rank_r(d)).  Items that several retrievers
    agree on rise to the top; an item only one retriever liked still survives.

    Duplicate ids inside one ranking count once, at their best rank.
    """
    scores: Dict[Hashable, float] = defaultdict(float)
    for name, ranking in rankings.items():
        weight = 1.0 if weights is None else weights.get(name, 1.0)
        seen = set()
        for position, item in enumerate(ranking, start=1):
            if item in seen:
                continue
            seen.add(item)
            scores[item] += weight / (k + position)
    # Stable tiebreak on insertion order via sort stability + id repr.
    return sorted(scores.items(), key=lambda pair: (-pair[1], repr(pair[0])))


def collapse_to_units(
    passage_ranking: Sequence[Tuple[Hashable, Hashable]],
) -> List[Hashable]:
    """
    Turn a ranking of (passage_id, unit_id) into a ranking of unit ids.

    Several passages of the same section often match; the unit should be
    ranked once, at the position of its best passage.
    """
    seen, units = set(), []
    for _, unit_id in passage_ranking:
        if unit_id not in seen:
            seen.add(unit_id)
            units.append(unit_id)
    return units


def fit_to_budget(
    texts: Sequence[str], char_budget: int, separator_chars: int = 7
) -> Tuple[List[str], bool]:
    """
    Keep whole units, in rank order, until the budget is spent.

    The old retriever sliced the *joined* string at 18,000 characters, which
    could cut a unit mid-sentence and always cost the lowest-ranked unit its
    tail. Here a unit is either included whole or (if it is the only one that
    fits at all) truncated at a paragraph boundary. Returns (texts, truncated).
    """
    if char_budget <= 0:
        return [], bool(texts)

    chosen: List[str] = []
    used = 0
    truncated = False
    for text in texts:
        cost = len(text) + (separator_chars if chosen else 0)
        if used + cost <= char_budget:
            chosen.append(text)
            used += cost
            continue
        truncated = True
        if not chosen:
            window = text[:char_budget]
            cut = window.rfind("\n\n")
            chosen.append(window[:cut] if cut > char_budget * 0.5 else window)
        break
    return chosen, truncated
