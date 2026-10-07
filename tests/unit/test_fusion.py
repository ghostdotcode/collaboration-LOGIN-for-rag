from rag.fusion import collapse_to_units, fit_to_budget, rrf_fuse


def ids(result):
    return [i for i, _ in result]


class TestRRF:
    def test_agreement_beats_single_vote(self):
        out = rrf_fuse({"dense": ["a", "b", "c"], "lex": ["c", "b", "d"]})
        assert ids(out)[0] in {"b", "c"}          # both retrievers rank b and c
        assert set(ids(out)) == {"a", "b", "c", "d"}

    def test_item_found_by_one_retriever_survives(self):
        assert "z" in ids(rrf_fuse({"dense": ["a"], "lex": ["z"]}))

    def test_duplicates_in_one_ranking_count_once(self):
        once = dict(rrf_fuse({"r": ["a", "b"]}))
        dup = dict(rrf_fuse({"r": ["a", "a", "a", "b"]}))
        assert once["a"] == dup["a"]

    def test_weights(self):
        out = rrf_fuse({"x": ["a"], "y": ["b"]}, weights={"x": 0.1, "y": 1.0})
        assert ids(out)[0] == "b"

    def test_empty(self):
        assert rrf_fuse({}) == []
        assert rrf_fuse({"x": []}) == []

    def test_deterministic_ties(self):
        a = ids(rrf_fuse({"x": ["a"], "y": ["b"]}))
        assert a == ids(rrf_fuse({"y": ["b"], "x": ["a"]}))


def test_collapse_keeps_best_passage_position():
    assert collapse_to_units([(1, "u1"), (2, "u1"), (3, "u2"), (4, "u1")]) == ["u1", "u2"]


class TestBudget:
    def test_whole_units_only(self):
        out, truncated = fit_to_budget(["a" * 100, "b" * 100, "c" * 100], 250)
        assert out == ["a" * 100, "b" * 100] and truncated

    def test_everything_fits(self):
        out, truncated = fit_to_budget(["a", "b"], 100)
        assert out == ["a", "b"] and not truncated

    def test_first_unit_bigger_than_budget_is_cut_at_paragraph(self):
        text = ("x" * 60 + "\n\n") * 5
        out, truncated = fit_to_budget([text], 150)
        assert truncated and len(out[0]) <= 150 and not out[0].endswith("\n\n" + "x" * 5)

    def test_zero_budget_and_empty(self):
        assert fit_to_budget(["a"], 0) == ([], True)
        assert fit_to_budget([], 100) == ([], False)
