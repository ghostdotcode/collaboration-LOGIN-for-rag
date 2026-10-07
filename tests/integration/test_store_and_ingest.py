"""Store + ingestion against a real Postgres/pgvector (throwaway `rag_test` database)."""

import os

import pymupdf
import pytest
from sqlalchemy import text

from ingestion import pipeline
from ingestion.pipeline import IngestionError, build_from_pdf, index_document
from rag.okf import KnowledgeDocument, KnowledgeUnit, normalise_units, stable_id
from rag.store import Store

pytestmark = pytest.mark.db

URL = os.getenv("RAG_TEST_DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5433/rag_test")
DIM = 8


class FakeEmbedder:
    model_name = "fake-8d"
    dim = DIM

    @staticmethod
    def _vec(t: str):
        # deterministic bag-of-keywords vector so ranking is predictable
        keys = ["annual", "maternity", "overtime", "sick", "travel", "probation", "notice", "holiday"]
        v = [float(t.lower().count(k)) for k in keys]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed_passages(self, texts, batch_size=32):
        return [self._vec(t) for t in texts]

    def embed_query(self, q):
        return self._vec(q)


@pytest.fixture(autouse=True)
def _okf_to_tmp(tmp_path, monkeypatch):
    """Never write test documents into the real data/okf directory."""
    monkeypatch.setattr(pipeline.Config, "OKF_DIR", tmp_path / "okf")


@pytest.fixture()
def store():
    try:
        s = Store(URL, DIM)
        s.ensure_schema()
    except Exception as exc:
        pytest.skip(f"rag_test database unavailable: {exc}")
    with s.engine.begin() as conn:
        conn.execute(text("TRUNCATE okf_documents CASCADE"))
    yield s
    with s.engine.begin() as conn:
        conn.execute(text("TRUNCATE okf_documents CASCADE"))


def make_doc(doc_id="d1", sha="a" * 64, sections=None):
    sections = sections or {
        "Annual": ("Annual leave is twenty one days per leave year for permanent staff. " * 3, 3),
        "Maternity": ("Maternity leave is three months on full pay for eligible employees. " * 3, 4),
        "Overtime": ("Overtime on a Sunday is paid at double the normal hourly rate. " * 3, 9),
    }
    units = []
    for i, (name, (body, page)) in enumerate(sections.items()):
        units.append(KnowledgeUnit(unit_id=stable_id(doc_id, name), doc_id=doc_id, ord=i, page_start=page,
                                   page_end=page, section_path=["Policy", name], text=body, passages=[body]))
    return KnowledgeDocument(doc_id=doc_id, title="Manual", sha256=sha, source_path="/x.pdf", n_pages=10,
                             units=normalise_units(units))


class TestStore:
    def test_schema_is_idempotent(self, store):
        store.ensure_schema(); store.ensure_schema()
        assert store.stats() == {"documents": 0, "units": 0, "passages": 0}

    def test_index_search_roundtrip(self, store):
        emb = FakeEmbedder()
        assert index_document(make_doc(), store, emb)["status"] == "created"
        hits = store.dense_search(emb.embed_query("maternity"), 3)
        assert store.get_units([hits[0].unit_id])[hits[0].unit_id].heading == "Policy > Maternity"
        assert store.get_document("d1")["n_pages"] == 10

    def test_unchanged_document_is_skipped(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        assert index_document(make_doc(), store, emb)["status"] == "unchanged"

    def test_changed_document_replaces_without_duplicates(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        r = index_document(make_doc(sha="b" * 64, sections={"Annual": ("Annual leave is now thirty days. " * 4, 1)}), store, emb)
        assert r["status"] == "replaced" and store.stats()["units"] == 1

    def test_force_reindexes_even_if_unchanged(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        assert index_document(make_doc(), store, emb, force=True)["status"] == "replaced"

    def test_embedding_model_change_triggers_reindex(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        other = FakeEmbedder(); other.model_name = "other-model"
        assert index_document(make_doc(), store, other)["status"] == "replaced"

    def test_failed_replacement_is_atomic_old_data_survives(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        before = store.stats()

        class Broken(FakeEmbedder):
            def embed_passages(self, texts, batch_size=32):
                return [[0.0] * (DIM + 5) for _ in texts]    # wrong dimension -> INSERT fails mid-transaction
        with pytest.raises(Exception):
            index_document(make_doc(sha="c" * 64), store, Broken())
        assert store.stats() == before                            # old version fully intact
        assert store.document_state("d1")["sha256"] == "a" * 64

    def test_empty_document_never_wipes_existing_knowledge(self, store):
        emb = FakeEmbedder()
        index_document(make_doc(), store, emb)
        empty = KnowledgeDocument(doc_id="d1", title="Manual", sha256="z" * 64, source_path="/x.pdf", units=[])
        with pytest.raises(IngestionError):
            index_document(empty, store, emb)
        assert store.stats()["units"] == 3

    def test_delete_cascades(self, store):
        index_document(make_doc(), store, FakeEmbedder())
        assert store.delete_document("d1") == 1
        assert store.stats() == {"documents": 0, "units": 0, "passages": 0}
        assert store.delete_document("d1") == 0

    def test_units_on_pages(self, store):
        index_document(make_doc(), store, FakeEmbedder())
        assert len(store.units_on_pages("d1", [3, 9])) == 2
        assert store.units_on_pages("d1", [99]) == [] and store.units_on_pages("d1", []) == []
        assert store.units_on_pages("other", [3]) == []

    def test_two_documents_are_isolated_on_delete(self, store):
        emb = FakeEmbedder()
        index_document(make_doc("d1"), store, emb)
        index_document(make_doc("d2", sha="d" * 64), store, emb)
        store.delete_document("d1")
        assert store.stats()["documents"] == 1 and store.get_document("d2")


class TestLexical:
    @pytest.fixture(autouse=True)
    def _seed(self, store):
        index_document(make_doc(), store, FakeEmbedder())
        self.store = store

    def test_finds_exact_term(self):
        assert self.store.lexical_search("maternity", 5)

    @pytest.mark.parametrize("hostile", [
        "'; DROP TABLE okf_passages; --", "a & ! | ( ) : * <-> b", "\\", "\"unterminated", "%%%", "\x00null",
        "the and of a", "", "   ", "日本語のみ", "x" * 5000,
    ])
    def test_hostile_or_degenerate_input_never_raises(self, hostile):
        self.store.lexical_search(hostile, 5)
        self.store.lexical_search(hostile, 5, max_df_ratio=0.5)
        assert self.store.stats()["passages"] == 3          # table still intact

    def test_distinctive_terms_drop_common_words(self):
        # "leave" appears in 2 of 3 passages, "overtime" in 1
        assert self.store.distinctive_terms("leave overtime", 0.4) == ["overtim"]

    def test_distinctive_search_only_matches_rare_words(self):
        hits = self.store.lexical_search("leave overtime", 5, max_df_ratio=0.4)
        assert len(hits) == 1

    def test_df_cache_is_invalidated_by_new_documents(self):
        self.store.distinctive_terms("overtime", 1.0)
        index_document(make_doc("d2", sha="e" * 64, sections={"X": ("overtime overtime pay is calculated as follows " * 3, 1)}),
                       self.store, FakeEmbedder())
        assert self.store._df is None


@pytest.fixture()
def pdf_factory(tmp_path):
    def make(name, pages):
        doc = pymupdf.open()
        for body in pages:
            page = doc.new_page()
            if body:
                page.insert_textbox(pymupdf.Rect(50, 50, 550, 780), body, fontsize=11)
        path = tmp_path / name
        doc.save(path)
        return path
    return make


class TestPdfIngestion:
    def test_pages_are_tracked_per_section(self, pdf_factory):
        p1 = "Annual leave rules apply to all permanent employees of the company across every department. " * 4
        p2 = "Maternity leave is granted for three months on full pay to every eligible employee at the company. " * 4
        doc = build_from_pdf(pdf_factory("m.pdf", [p1, p2]), "Manual")
        assert doc.n_pages == 2 and doc.units and all(u.page_start for u in doc.units)
        assert doc.units[0].page_start == 1 and doc.units[-1].page_end == 2

    def test_blank_pdf_yields_no_units_and_is_refused(self, pdf_factory, store):
        doc = build_from_pdf(pdf_factory("blank.pdf", ["", ""]), "Blank")
        assert doc.units == []
        with pytest.raises(IngestionError):
            index_document(doc, store, FakeEmbedder())

    def test_corrupt_pdf_raises(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
        with pytest.raises(Exception):
            build_from_pdf(bad, "Bad")

    def test_same_content_gives_same_ids(self, pdf_factory):
        body = ["Overtime on Sundays is paid at double the hourly rate for approved hours worked. " * 5]
        a = build_from_pdf(pdf_factory("a.pdf", body), "T")
        b = build_from_pdf(pdf_factory("a.pdf", body), "T")
        assert [u.unit_id for u in a.units] == [u.unit_id for u in b.units]
