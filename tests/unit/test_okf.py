import json

import pytest

from rag.okf import (
    KnowledgeDocument, KnowledgeUnit, clean_summary, normalise_units, read_okf, stable_id, write_okf,
)

BODY = "Employees are entitled to 21 working days of annual leave per leave year. " * 2


def unit(text=BODY, passages=None, ord=0, doc="d1"):
    return KnowledgeUnit(unit_id=stable_id(doc, text), doc_id=doc, ord=ord, text=text,
                         passages=passages if passages is not None else [text])


class TestCleanSummary:
    def test_none_and_blank(self):
        assert clean_summary(None) is None
        assert clean_summary("   ") is None
        assert clean_summary("[Context: ]") is None

    def test_keeps_first_sentence_only(self):
        assert clean_summary("[Context: Leave rules. More detail here.]") == "Leave rules."

    def test_strips_markdown(self):
        assert clean_summary("**Annual** leave | `rules`") == "Annual leave rules"

    def test_overlong_generation_is_dropped(self):
        # 554/587 legacy headers looked like this: a giant entity list with no sentence break
        assert clean_summary("**Entities:** " + "word " * 120) is None


class TestNormaliseUnits:
    def test_drops_heading_stub(self):
        assert normalise_units([unit("# HUMAN RESOURCES POLICY MANUAL", ["# HUMAN"])]) == []

    def test_drops_exact_duplicates_and_renumbers(self):
        out = normalise_units([unit(), unit(), unit(BODY + " Extra sentence for a distinct unit.", ord=7)])
        assert [u.ord for u in out] == [0, 1]

    def test_tiny_passage_is_merged_not_lost(self):
        out = normalise_units([unit(BODY, [BODY, "Note."])])
        assert out[0].passages == [BODY.strip() + "\nNote."]

    def test_no_passages_falls_back_to_whole_text(self):
        assert normalise_units([unit(BODY, [])])[0].passages == [BODY.strip()]

    def test_blank_text_rejected_by_model(self):
        with pytest.raises(ValueError):
            KnowledgeUnit(unit_id="x", doc_id="d", ord=0, text="   ")

    def test_negative_page_rejected(self):
        with pytest.raises(ValueError):
            KnowledgeUnit(unit_id="x", doc_id="d", ord=0, text="t", page_start=0)


class TestEmbeddingText:
    def test_breadcrumb_and_summary_are_prepended(self):
        u = unit()
        u.section_path, u.summary = ["Leave", "Annual"], "Annual leave entitlement."
        text = u.passage_for_embedding("body", "HR Manual")
        assert text.startswith("HR Manual > Leave > Annual\nAnnual leave entitlement.\nbody")

    def test_no_section_path(self):
        assert unit().passage_for_embedding("body", "HR Manual") == "HR Manual\nbody"


class TestRoundTrip:
    def make(self):
        return KnowledgeDocument(doc_id="d1", title="T", sha256="abc", source_path="/x.pdf", n_pages=3,
                                 units=normalise_units([unit()]))

    def test_write_then_read(self, tmp_path):
        path = write_okf(self.make(), tmp_path)
        back = read_okf(path)
        assert back.units[0].text == BODY.strip() and back.title == "T"

    def test_unicode_survives(self, tmp_path):
        text = "Résumé policy — “smart quotes” and 日本語 text that is long enough to be kept as a unit. " * 2
        doc = KnowledgeDocument(doc_id="d", title="É", sha256="a", source_path="x", units=[unit(text)])
        assert read_okf(write_okf(doc, tmp_path)).units[0].text == text

    def test_empty_file(self, tmp_path):
        p = tmp_path / "e.okf.jsonl"
        p.write_text("")
        with pytest.raises(ValueError, match="empty"):
            read_okf(p)

    def test_missing_header(self, tmp_path):
        p = tmp_path / "e.okf.jsonl"
        p.write_text(json.dumps({"hello": 1}) + "\n")
        with pytest.raises(ValueError, match="header"):
            read_okf(p)

    def test_future_major_version_refused(self, tmp_path):
        p = tmp_path / "e.okf.jsonl"
        p.write_text(json.dumps({"_okf": {"okf_version": "9.0"}}) + "\n")
        with pytest.raises(ValueError, match="version"):
            read_okf(p)

    def test_corrupt_unit_line(self, tmp_path):
        path = write_okf(self.make(), tmp_path)
        path.write_text(path.read_text() + "{not json\n")
        with pytest.raises(ValueError):
            read_okf(path)


def test_stable_id_is_deterministic_and_order_sensitive():
    assert stable_id("a", "b") == stable_id("a", "b")
    assert stable_id("a", "b") != stable_id("b", "a")
    assert stable_id("ab", "") != stable_id("a", "b")
