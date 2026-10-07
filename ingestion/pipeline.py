"""
Ingestion pipeline: PDF -> OKF -> Postgres.

    python -m ingestion.pipeline ingest data/raw/company_policy.pdf --title "HR Policy Manual"
    python -m ingestion.pipeline migrate-legacy data/raw/company_policy.pdf

What changed relative to the old `test_ingestion.py` + `fast_index.py` scripts
(which were *scripts named like tests*, and ran every step every time):

  * page-aware parsing, so every section knows its pages (-> citations)
  * incremental: an unchanged file (same SHA-256, OKF version, embedding model)
    is skipped; a changed one is replaced atomically, never wiped wholesale
  * coreference resolution and LLM enrichment are OPT-IN. Coref rewrites
    pronouns in legal text (risky for policy wording) and takes the longest;
    enrichment was a paid LLM call per section whose output mostly violated its
    own length contract. A deterministic heading breadcrumb replaces it.
  * output is validated OKF, not an untyped blob with duplicated parent text
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Config  # noqa: E402
from rag.okf import (  # noqa: E402
    OKF_VERSION,
    KnowledgeDocument,
    KnowledgeUnit,
    clean_summary,
    normalise_units,
    sha256_file,
    slugify,
    stable_id,
    write_okf,
)
from rag.pages import build_page_index, locate_pages  # noqa: E402

logger = logging.getLogger("ingestion")


class IngestionError(RuntimeError):
    """The document cannot be indexed; the message is safe to show an operator."""

PAGE_MARK = "\n\n<!--PAGE:{n}-->\n\n"
_PAGE_RE = re.compile(r"<!--PAGE:(\d+)-->")
_HEADER_KEYS = ("Header 1", "Header 2", "Header 3")


def _section_path(metadata: Dict[str, str]) -> List[str]:
    parts = [re.sub(r"\s+", " ", metadata[k]).strip() for k in _HEADER_KEYS if metadata.get(k)]
    # A bare number is a PDF page number the parser mistook for a heading.
    return [p for p in parts if not p.isdigit()]


def pdf_page_texts(pdf: Path) -> List[str]:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return [page.get_text() for page in doc]


def build_from_pdf(pdf: Path, title: str, enrich: bool = False, coref: bool = False) -> KnowledgeDocument:
    """Parse -> (coref) -> chunk -> (enrich) -> OKF, preserving page numbers."""
    import pymupdf4llm

    from ingestion.chunker import HierarchicalChunker

    pages = pymupdf4llm.to_markdown(str(pdf), page_chunks=True)
    marked = "".join(PAGE_MARK.format(n=i) + p["text"] for i, p in enumerate(pages, start=1))

    if coref:
        from ingestion.coref import CorefProcessor

        marked = CorefProcessor().resolve_text(marked)

    chunks = HierarchicalChunker().chunk_document(marked)

    enricher = None
    if enrich:
        from ingestion.enricher import ContextEnricher

        enricher = ContextEnricher()

    doc_id = slugify(pdf.stem)
    units: List[KnowledgeUnit] = []
    current_page = 1
    for index, group in enumerate(chunks):
        text = group["parent_content"]
        markers = [int(m) for m in _PAGE_RE.findall(text)]
        starts_on_marker = _PAGE_RE.match(text.lstrip()) is not None
        page_start = markers[0] if (markers and starts_on_marker) else current_page
        page_end = markers[-1] if markers else page_start
        current_page = page_end

        clean_text = _PAGE_RE.sub("", text).strip()
        passages = [_PAGE_RE.sub("", c).strip() for c in group["child_chunks"]]
        summary = None
        if enricher is not None:
            try:
                summary = clean_summary(
                    enricher.chain.invoke({"document_context": clean_text[:2000]}).content
                )
            except Exception as exc:
                logger.warning("enrichment failed for section %d: %s", index, exc)

        units.append(
            KnowledgeUnit(
                unit_id=stable_id(doc_id, str(index), clean_text[:200]),
                doc_id=doc_id,
                ord=index,
                page_start=page_start,
                page_end=max(page_start, page_end),
                section_path=_section_path(group["metadata"]),
                text=clean_text or " ",
                summary=summary,
                passages=[p for p in passages if p],
            )
        )

    return KnowledgeDocument(
        doc_id=doc_id,
        title=title,
        sha256=sha256_file(pdf),
        source_path=str(pdf),
        n_pages=len(pages),
        units=normalise_units(units),
    )


def build_from_legacy(legacy_json: Path, pdf: Path, title: str) -> KnowledgeDocument:
    """
    Convert the already-enriched legacy JSON into OKF *without* repeating any
    paid LLM work: reuse its sections, drop the bloated context headers (keep a
    summary only if it honours the original length contract) and recover page
    numbers by text overlap against the PDF.
    """
    data = json.loads(legacy_json.read_text(encoding="utf-8"))
    page_index = build_page_index(pdf_page_texts(pdf))
    doc_id = slugify(pdf.stem)
    header_re = re.compile(r"^\[Context:\s*(.*?)\]\s", re.S)

    units: List[KnowledgeUnit] = []
    for index, group in enumerate(data):
        text = group["parent_content"].strip()
        summary = None
        enriched = group.get("enriched_child_chunks") or []
        if enriched:
            m = header_re.match(enriched[0])
            summary = clean_summary(m.group(1)) if m else None
        span = locate_pages(text, page_index)
        units.append(
            KnowledgeUnit(
                unit_id=stable_id(doc_id, str(index), text[:200]),
                doc_id=doc_id,
                ord=index,
                page_start=span[0] if span else None,
                page_end=span[1] if span else None,
                section_path=_section_path(group.get("metadata", {})),
                text=text or " ",
                summary=summary,
                passages=list(group.get("child_chunks", [])),
                flags=[] if span else ["page_unknown"],
            )
        )

    return KnowledgeDocument(
        doc_id=doc_id,
        title=title,
        sha256=sha256_file(pdf),
        source_path=str(pdf),
        n_pages=len(page_index),
        units=normalise_units(units),
    )


def index_document(
    doc: KnowledgeDocument, store, embedder, force: bool = False, okf_dir: Optional[Path] = None
) -> Dict[str, object]:
    """Embed and store a document; skip it if nothing has changed."""
    if not doc.units:
        # A scanned (image-only) or empty PDF yields no text. Replacing the stored
        # document with nothing would silently wipe a *working* knowledge base.
        raise IngestionError(
            f"{doc.title!r} produced no extractable text (scanned or empty PDF?). "
            "Nothing was changed; OCR the file or use the visual index."
        )
    state = store.document_state(doc.doc_id)
    if (
        state
        and not force
        and state["sha256"] == doc.sha256
        and state["okf_version"] == OKF_VERSION
        and state["embed_model"] == embedder.model_name
    ):
        return {"doc_id": doc.doc_id, "status": "unchanged", **doc.stats()}

    started = time.perf_counter()
    vectors: Dict[str, List[List[float]]] = {}
    for unit in doc.units:
        texts = [unit.passage_for_embedding(p, doc.title) for p in unit.passages]
        vectors[unit.unit_id] = embedder.embed_passages(texts)
    store.replace_document(doc, vectors, embedder.model_name)
    write_okf(doc, okf_dir or Config.OKF_DIR)
    return {
        "doc_id": doc.doc_id,
        "status": "replaced" if state else "created",
        "seconds": round(time.perf_counter() - started, 1),
        **doc.stats(),
    }


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="parse a PDF and index it")
    p_ing.add_argument("pdf", type=Path)
    p_ing.add_argument("--title")
    p_ing.add_argument("--enrich", action="store_true", help="LLM section summaries (paid, slow)")
    p_ing.add_argument("--coref", action="store_true", help="resolve pronouns (slow, rewrites wording)")
    p_ing.add_argument("--force", action="store_true")

    p_mig = sub.add_parser("migrate-legacy", help="convert the existing enriched JSON, no LLM calls")
    p_mig.add_argument("pdf", type=Path)
    p_mig.add_argument("--legacy-json", type=Path, default=Config.PROCESSED_DIR / "final_enriched_chunks.json")
    p_mig.add_argument("--title")
    p_mig.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    if not args.pdf.exists():
        print(f"error: {args.pdf} not found", file=sys.stderr)
        return 2
    title = args.title or args.pdf.stem.replace("_", " ").title()

    from rag.embedding import Embedder
    from rag.store import Store

    store = Store(Config.DATABASE_URL, Config.EMBED_DIM)
    store.ensure_schema()

    try:
        if args.cmd == "ingest":
            doc = build_from_pdf(args.pdf, title, enrich=args.enrich, coref=args.coref)
        else:
            doc = build_from_legacy(args.legacy_json, args.pdf, title)

        print(f"[OKF] {doc.stats()}")
        embedder = Embedder()
        print(json.dumps(index_document(doc, store, embedder, force=args.force), indent=2))
    except IngestionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # corrupt/encrypted PDF, unreadable JSON, ...
        print(f"error: could not ingest {args.pdf.name}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
