import pytest

from rag.store import UnitRow, lexical_query, vector_literal


class TestLexicalQuery:
    def test_or_of_content_words(self):
        assert lexical_query("maternity leave days") == "maternity | leave | days"

    def test_dedupes(self):
        assert lexical_query("leave leave LEAVE") == "leave"

    @pytest.mark.parametrize("hostile", [
        "a'; DROP TABLE okf_passages; --", "foo & !bar | (baz) :*", "x\\y", "\"quoted\"", "a <-> b",
    ])
    def test_tsquery_operators_cannot_survive(self, hostile):
        q = lexical_query(hostile)
        assert not any(ch in q.replace(" | ", " ") for ch in "&!():*<>'\"\\;-")

    def test_empty_and_symbol_only(self):
        assert lexical_query("") == ""
        assert lexical_query("?!@#") == ""
        assert lexical_query("a b c") == ""      # single characters are noise

    def test_term_cap(self):
        q = lexical_query(" ".join(f"word{i}" for i in range(100)))
        assert len(q.split(" | ")) == 24


def test_vector_literal():
    assert vector_literal([1, 0.5, -0.25]) == "[1.000000,0.500000,-0.250000]"


class TestCitationLabel:
    def row(self, **kw):
        base = dict(unit_id="u", doc_id="d", doc_title="Manual", ord=0, page_start=None, page_end=None,
                    section_path=[], text="t", summary=None)
        return UnitRow(**{**base, **kw})

    def test_full(self):
        assert self.row(page_start=28, page_end=28, section_path=["Leave", "Maternity"]).citation_label() \
            == "Manual, p. 28 (Leave > Maternity)"

    def test_range(self):
        assert "pp. 3-5" in self.row(page_start=3, page_end=5).citation_label()

    def test_unknown_page_and_no_heading(self):
        assert self.row().citation_label() == "Manual"


class TestHeadingCleanup:
    def row(self, path):
        return UnitRow("u", "d", "Manual", 0, 5, 5, path, "t", None)

    def test_page_number_headings_and_title_root_are_dropped(self):
        r = self.row(["HUMAN RESOURCES POLICY MANUAL", "SECTION FIVE: LEAVE", "27"])
        assert r.heading == "SECTION FIVE: LEAVE"
        assert r.citation_label() == "Manual, p. 5 (SECTION FIVE: LEAVE)"

    def test_only_numeric_path_gives_no_parentheses(self):
        assert self.row(["12"]).citation_label() == "Manual, p. 5"
