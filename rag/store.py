"""
Postgres storage for OKF knowledge: pgvector (dense) + full-text (lexical).

Replaces the LangChain `PGVector` collection, which:
  * stored the full parent text inside every child row's JSON metadata,
  * had no keyword index and no ANN index, and
  * was rebuilt from scratch (`pre_delete_collection=True`) on every ingest.

Layout
  okf_documents  one row per source file (+ hash, embedding model, version)
  okf_units      the sections the LLM reads (text stored ONCE)
  okf_passages   the spans that are embedded; vector(dim) + generated tsvector
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from rag.okf import KnowledgeDocument

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[A-Za-z0-9]{2,}")
_LEXEME = re.compile(r"[a-z0-9]+")


def lexical_query(question: str, max_terms: int = 24) -> str:
    """
    Build a safe OR-style tsquery from free text.

    Postgres' `websearch_to_tsquery` ANDs every term, so a natural-language
    question ("what is the policy for maternity leave?") matches nothing. We
    OR the content words instead and let `ts_rank_cd` rank by how many match.
    Only [A-Za-z0-9] tokens survive, which also makes injection through
    tsquery operators (& | ! : ( )) impossible by construction.
    """
    seen, terms = set(), []
    for token in _TOKEN.findall(question.lower()):
        if token not in seen:
            seen.add(token)
            terms.append(token)
    return " | ".join(terms[:max_terms])


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


@dataclass
class PassageHit:
    passage_id: int
    unit_id: str
    score: float


@dataclass
class UnitRow:
    unit_id: str
    doc_id: str
    doc_title: str
    ord: int
    page_start: Optional[int]
    page_end: Optional[int]
    section_path: List[str]
    text: str
    summary: Optional[str]

    @property
    def heading(self) -> str:
        return " > ".join(self.section_path)

    def citation_label(self) -> str:
        pages = ""
        if self.page_start:
            pages = (
                f", p. {self.page_start}"
                if not self.page_end or self.page_end == self.page_start
                else f", pp. {self.page_start}-{self.page_end}"
            )
        where = f" ({self.heading})" if self.heading else ""
        return f"{self.doc_title}{pages}{where}"


class Store:
    def __init__(self, db_url: str, dim: int = 1024):
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        # Async drivers would be wrong here: retrieval runs in worker threads.
        self.engine: Engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        self.dim = dim
        self._df: Optional[Tuple[int, Dict[str, int]]] = None   # (n_passages, lexeme -> passages containing it)

    # ── schema ────────────────────────────────────────────────────────────
    def ensure_schema(self) -> None:
        statements = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            """CREATE TABLE IF NOT EXISTS okf_documents (
                doc_id text PRIMARY KEY, title text NOT NULL, sha256 text NOT NULL,
                source_path text, n_pages int, okf_version text NOT NULL,
                embed_model text NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now())""",
            """CREATE TABLE IF NOT EXISTS okf_units (
                unit_id text PRIMARY KEY,
                doc_id text NOT NULL REFERENCES okf_documents(doc_id) ON DELETE CASCADE,
                ord int NOT NULL, page_start int, page_end int,
                section_path text[] NOT NULL DEFAULT '{}',
                text text NOT NULL, summary text, flags text[] NOT NULL DEFAULT '{}')""",
            f"""CREATE TABLE IF NOT EXISTS okf_passages (
                passage_id bigserial PRIMARY KEY,
                unit_id text NOT NULL REFERENCES okf_units(unit_id) ON DELETE CASCADE,
                doc_id text NOT NULL, ord int NOT NULL, text text NOT NULL,
                embedding vector({self.dim}) NOT NULL,
                tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED)""",
            "CREATE INDEX IF NOT EXISTS okf_units_doc_idx ON okf_units (doc_id, ord)",
            "CREATE INDEX IF NOT EXISTS okf_passages_unit_idx ON okf_passages (unit_id)",
            "CREATE INDEX IF NOT EXISTS okf_passages_tsv_idx ON okf_passages USING gin (tsv)",
            "CREATE INDEX IF NOT EXISTS okf_passages_hnsw_idx ON okf_passages "
            "USING hnsw (embedding vector_cosine_ops)",
        ]
        with self.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))

    # ── ingestion ─────────────────────────────────────────────────────────
    def document_state(self, doc_id: str) -> Optional[Dict[str, str]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT sha256, okf_version, embed_model FROM okf_documents WHERE doc_id=:d"),
                {"d": doc_id},
            ).mappings().first()
        return dict(row) if row else None

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT doc_id, title, source_path, n_pages FROM okf_documents WHERE doc_id=:d"),
                {"d": doc_id},
            ).mappings().first()
        return dict(row) if row else None

    def replace_document(
        self,
        doc: KnowledgeDocument,
        passage_vectors: Dict[str, List[List[float]]],
        embed_model: str,
    ) -> None:
        """
        Atomically swap a document's knowledge. Readers see either the old
        version or the new one, never a half-written mix, because the delete
        and all inserts commit in one transaction.
        """
        self._df = None
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM okf_documents WHERE doc_id=:d"), {"d": doc.doc_id})
            conn.execute(
                text(
                    "INSERT INTO okf_documents (doc_id,title,sha256,source_path,n_pages,okf_version,embed_model)"
                    " VALUES (:doc_id,:title,:sha256,:source_path,:n_pages,:okf_version,:embed_model)"
                ),
                {**doc.model_dump(exclude={"units", "created_at"}), "embed_model": embed_model},
            )
            for unit in doc.units:
                conn.execute(
                    text(
                        "INSERT INTO okf_units (unit_id,doc_id,ord,page_start,page_end,section_path,text,summary,flags)"
                        " VALUES (:unit_id,:doc_id,:ord,:page_start,:page_end,:section_path,:text,:summary,:flags)"
                    ),
                    unit.model_dump(exclude={"passages"}),
                )
                vectors = passage_vectors[unit.unit_id]
                for position, (passage, vec) in enumerate(zip(unit.passages, vectors)):
                    conn.execute(
                        text(
                            "INSERT INTO okf_passages (unit_id,doc_id,ord,text,embedding)"
                            " VALUES (:u,:d,:o,:t,CAST(:e AS vector))"
                        ),
                        {
                            "u": unit.unit_id,
                            "d": doc.doc_id,
                            "o": position,
                            "t": unit.passage_for_embedding(passage, doc.title),
                            "e": vector_literal(vec),
                        },
                    )

    def delete_document(self, doc_id: str) -> int:
        self._df = None
        with self.engine.begin() as conn:
            return conn.execute(text("DELETE FROM okf_documents WHERE doc_id=:d"), {"d": doc_id}).rowcount

    # ── retrieval ─────────────────────────────────────────────────────────
    def dense_search(self, query_vec: Sequence[float], n: int) -> List[PassageHit]:
        sql = text(
            "SELECT passage_id, unit_id, 1 - (embedding <=> CAST(:q AS vector)) AS score "
            "FROM okf_passages ORDER BY embedding <=> CAST(:q AS vector) LIMIT :n"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"q": vector_literal(query_vec), "n": n}).all()
        return [PassageHit(r[0], r[1], float(r[2])) for r in rows]

    def _document_frequencies(self) -> Tuple[int, Dict[str, int]]:
        if self._df is None:
            with self.engine.connect() as conn:
                total = conn.execute(text("SELECT count(*) FROM okf_passages")).scalar_one()
                rows = conn.execute(
                    text("SELECT word, ndoc FROM ts_stat('SELECT tsv FROM okf_passages')")
                ).all()
            self._df = (int(total), {r[0]: int(r[1]) for r in rows})
        return self._df

    def distinctive_terms(self, question: str, max_df_ratio: float) -> List[str]:
        """
        Stemmed query terms that are *rare* in the corpus.

        In an HR manual nearly every question contains "leave", "days", "employee";
        matching on those pulls in half the document and, once fused with the
        dense ranking, measurably *hurt* recall (see eval). Keeping only terms that
        appear in <= max_df_ratio of passages leaves the words that actually
        identify a topic or an exact token ("NSSF", "Madaraka", "Form 12").
        """
        cleaned = " ".join(_TOKEN.findall(question.lower())[:48])
        if not cleaned:
            return []
        with self.engine.connect() as conn:
            lexemes = [
                r[0]
                for r in conn.execute(
                    text("SELECT unnest(tsvector_to_array(to_tsvector('english', :q)))"), {"q": cleaned}
                ).all()
            ]
        total, df = self._document_frequencies()
        keep = [
            w for w in lexemes
            if _LEXEME.fullmatch(w) and 0 < df.get(w, 0) <= max(1, int(total * max_df_ratio))
        ]
        return keep[:24]

    def lexical_search(self, question: str, n: int, max_df_ratio: float = 1.0) -> List[PassageHit]:
        if max_df_ratio < 1.0:
            terms = self.distinctive_terms(question, max_df_ratio)
            if not terms:
                return []
            tsq, config = " | ".join(terms), "simple"   # lexemes are already stemmed
        else:
            tsq, config = lexical_query(question), "english"
            if not tsq:
                return []
        sql = text(
            f"SELECT passage_id, unit_id, ts_rank_cd(tsv, q) AS score "
            f"FROM okf_passages, to_tsquery('{config}', :q) q "
            f"WHERE tsv @@ q ORDER BY score DESC LIMIT :n"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"q": tsq, "n": n}).all()
        return [PassageHit(r[0], r[1], float(r[2])) for r in rows]

    def get_units(self, unit_ids: Sequence[str]) -> Dict[str, UnitRow]:
        if not unit_ids:
            return {}
        sql = text(
            "SELECT u.unit_id,u.doc_id,d.title,u.ord,u.page_start,u.page_end,u.section_path,u.text,u.summary "
            "FROM okf_units u JOIN okf_documents d USING (doc_id) WHERE u.unit_id = ANY(:ids)"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"ids": list(unit_ids)}).all()
        return {r[0]: UnitRow(r[0], r[1], r[2], r[3], r[4], r[5], list(r[6] or []), r[7], r[8]) for r in rows}

    def units_on_pages(self, doc_id: str, pages: Sequence[int]) -> List[str]:
        """Unit ids whose page span overlaps any of the given pages."""
        if not pages:
            return []
        sql = text(
            "SELECT unit_id FROM okf_units WHERE doc_id=:d AND page_start IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM unnest(CAST(:pages AS int[])) AS p "
            "            WHERE p BETWEEN page_start AND coalesce(page_end, page_start)) "
            "ORDER BY ord"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"d": doc_id, "pages": list(pages)}).all()
        return [r[0] for r in rows]

    def passages_text(self, passage_ids: Sequence[int]) -> Dict[int, str]:
        if not passage_ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT passage_id, text FROM okf_passages WHERE passage_id = ANY(:ids)"),
                {"ids": list(passage_ids)},
            ).all()
        return {r[0]: r[1] for r in rows}

    # ── health ────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, int]:
        with self.engine.connect() as conn:
            return {
                "documents": conn.execute(text("SELECT count(*) FROM okf_documents")).scalar_one(),
                "units": conn.execute(text("SELECT count(*) FROM okf_units")).scalar_one(),
                "passages": conn.execute(text("SELECT count(*) FROM okf_passages")).scalar_one(),
            }

    def healthy(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # pragma: no cover
            logger.warning("store health check failed: %s", exc)
            return False
