"""
OKF - the Open Knowledge Format used between ingestion and retrieval.

NOTE ON THE NAME: "OKF" was requested without a definition. This module
implements it as a portable, versioned, provenance-carrying record format.
It is deliberately small so it can be reshaped if a different meaning was
intended; see docs/RAG_UPGRADE.md, section "OKF".

Why a format at all?  Before this, ingestion handed retrieval an untyped JSON
blob in which every child chunk re-embedded its full parent text inside its
own metadata (≈3x storage), carried no page numbers, no document identity and
no version. Nothing could be deduplicated, re-ingested incrementally, cited,
or validated. An OKF record fixes that:

    KnowledgeDocument   one source file (identity = SHA-256 of its bytes)
      └─ KnowledgeUnit  one coherent section ("parent"): the text the LLM reads
           └─ passages  small spans ("children"): the text that gets embedded

Every unit knows its document, pages and heading path, so answers can be cited
("Employee Handbook, p. 28, Leave > Maternity") and stale data can be replaced
atomically when a document changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from pydantic import BaseModel, Field, field_validator

OKF_VERSION = "1.0"

# A unit shorter than this is a heading stub ("# HUMAN RESOURCES POLICY MANUAL")
# that would only add retrieval noise.
MIN_UNIT_CHARS = 80
MIN_PASSAGE_CHARS = 40
MAX_SUMMARY_CHARS = 200


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "document"


def stable_id(*parts: str, length: int = 20) -> str:
    """Content-derived id: same input -> same id, so re-ingestion is idempotent."""
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


_MD_NOISE = re.compile(r"[*_`#>|]+")
_SPACE = re.compile(r"\s+")


def clean_summary(raw: Optional[str]) -> Optional[str]:
    """
    Normalise an LLM-written context summary.

    The previous enricher was asked for "one sentence, max 15 words" but 554 of
    587 headers came back as multi-hundred-character markdown entity lists
    (median 466 chars) that outweighed the passage they described. We keep at
    most the first sentence and hard-cap the length.
    """
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^\[?context:\s*", "", text, flags=re.I).rstrip("]")
    text = _SPACE.sub(" ", _MD_NOISE.sub(" ", text)).strip()
    if not text:
        return None
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(first) > MAX_SUMMARY_CHARS:
        return None  # an over-long "summary" is a failed generation: drop it
    return first


class KnowledgeUnit(BaseModel):
    unit_id: str
    doc_id: str
    ord: int = Field(ge=0)
    page_start: Optional[int] = Field(default=None, ge=1)
    page_end: Optional[int] = Field(default=None, ge=1)
    section_path: List[str] = Field(default_factory=list)
    text: str
    summary: Optional[str] = None
    passages: List[str] = Field(default_factory=list)
    flags: List[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("unit text is blank")
        return value

    @property
    def heading(self) -> str:
        return " > ".join(self.section_path)

    def passage_for_embedding(self, passage: str, doc_title: str) -> str:
        """
        What actually gets embedded: a short, *deterministic* breadcrumb plus
        the passage. The breadcrumb replaces the LLM header - it is free,
        always accurate, bounded in size, and gives dense *and* lexical search
        the section vocabulary ("LEAVE > MATERNITY").
        """
        crumb = " > ".join([doc_title, *self.section_path]) if self.section_path else doc_title
        summary = f"\n{self.summary}" if self.summary else ""
        return f"{crumb}{summary}\n{passage}"


class KnowledgeDocument(BaseModel):
    okf_version: str = OKF_VERSION
    doc_id: str
    title: str
    sha256: str
    source_path: str
    n_pages: Optional[int] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    units: List[KnowledgeUnit] = Field(default_factory=list)

    def stats(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "units": len(self.units),
            "passages": sum(len(u.passages) for u in self.units),
            "with_pages": sum(1 for u in self.units if u.page_start),
            "flagged": sum(1 for u in self.units if u.flags),
        }


# ── Quality gate ───────────────────────────────────────────────────────────


def normalise_units(units: Iterable[KnowledgeUnit]) -> List[KnowledgeUnit]:
    """
    Drop or repair low-value units before they reach the index.

    Removes heading-only stubs, drops passages too short to carry meaning
    (merging them into a neighbour so no text is lost), removes exact
    duplicate units, and re-numbers `ord`.
    """
    seen: set[str] = set()
    kept: List[KnowledgeUnit] = []
    for unit in units:
        body = unit.text.strip()
        if len(body) < MIN_UNIT_CHARS:
            continue
        digest = stable_id(unit.doc_id, body)
        if digest in seen:
            continue
        seen.add(digest)

        merged: List[str] = []
        for passage in unit.passages:
            passage = passage.strip()
            if not passage:
                continue
            if len(passage) < MIN_PASSAGE_CHARS and merged:
                merged[-1] = f"{merged[-1]}\n{passage}"
            else:
                merged.append(passage)
        if not merged:
            merged = [body]
        unit.passages = merged
        unit.text = body
        kept.append(unit)

    for index, unit in enumerate(kept):
        unit.ord = index
    return kept


# ── Serialisation ──────────────────────────────────────────────────────────


def write_okf(document: KnowledgeDocument, directory: Path) -> Path:
    """One JSONL file per document: a header line, then one unit per line."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document.doc_id}.okf.jsonl"
    header = document.model_dump(exclude={"units"})
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"_okf": header}, ensure_ascii=False) + "\n")
        for unit in document.units:
            handle.write(unit.model_dump_json() + "\n")
    return path


def read_okf(path: Path) -> KnowledgeDocument:
    """Load and *validate* an OKF file; raises ValueError on a malformed one."""
    with path.open("r", encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    if not lines:
        raise ValueError(f"{path.name}: empty OKF file")
    first = json.loads(lines[0])
    if "_okf" not in first:
        raise ValueError(f"{path.name}: missing _okf header line")
    header = first["_okf"]
    if header.get("okf_version", "").split(".")[0] != OKF_VERSION.split(".")[0]:
        raise ValueError(
            f"{path.name}: unsupported OKF version {header.get('okf_version')!r}"
        )
    units = [KnowledgeUnit.model_validate_json(line) for line in lines[1:]]
    return KnowledgeDocument(**header, units=units)
