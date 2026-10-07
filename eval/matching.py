"""Text normalisation for evaluating whether retrieved context contains an answer."""

from __future__ import annotations

import re
from typing import Iterable

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2011": "-", "\u2013": "-", "\u2014": "-"})


def normalise(text: str) -> str:
    text = text.translate(_QUOTES).lower()
    text = re.sub(r"[*_`#|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(context: str, phrases: Iterable[str]) -> bool:
    haystack = normalise(context)
    return any(normalise(p) in haystack for p in phrases)
