import json

import numpy as np
import pytest

from rag.visual import PageHit, VisualClient, VisualRetriever, VisualStore, maxsim


def unit_vec(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


class TestMaxSim:
    def test_each_query_token_takes_its_best_patch(self):
        q = np.array([[1, 0], [0, 1]], dtype=np.float32)
        page = np.array([[1, 0], [0, 1], [0.5, 0.5]], dtype=np.float32)
        assert maxsim(q, page) == pytest.approx(2.0)

    def test_better_page_scores_higher(self):
        q = np.array([[1, 0]], dtype=np.float32)
        assert maxsim(q, np.array([[1, 0]])) > maxsim(q, np.array([[0, 1]]))

    def test_float16_storage_is_fine(self):
        q = np.array([[1, 0]], dtype=np.float32)
        assert maxsim(q, np.array([[1, 0]], dtype=np.float16)) == pytest.approx(1.0)

    @pytest.mark.parametrize("q,p", [(np.zeros((0, 4)), np.ones((3, 4))), (np.ones((2, 4)), np.zeros((0, 4)))])
    def test_empty_is_zero_not_crash(self, q, p):
        assert maxsim(q, p) == 0.0


def build_store(tmp_path, pages):
    doc = tmp_path / "doc1"
    (doc / "vectors").mkdir(parents=True)
    (doc / "pages").mkdir()
    entries = []
    for n, arr in pages.items():
        np.save(doc / "vectors" / f"p{n:04d}.npy", arr)
        (doc / "pages" / f"p{n:04d}.jpg").write_bytes(b"\xff\xd8jpeg")
        entries.append({"page": n, "vectors": f"vectors/p{n:04d}.npy", "image": f"pages/p{n:04d}.jpg"})
    (doc / "manifest.json").write_text(json.dumps({"doc_id": "doc1", "pages": entries}))
    return VisualStore(tmp_path)


class TestVisualStore:
    def test_search_ranks_pages(self, tmp_path):
        store = build_store(tmp_path, {1: np.array([[0, 1]], np.float16), 2: np.array([[1, 0]], np.float16)})
        assert store.load() == 2
        hits = store.search(np.array([[1, 0]], np.float32), 5)
        assert [h.page for h in hits] == [2, 1]

    def test_empty_root(self, tmp_path):
        store = VisualStore(tmp_path)
        assert store.load() == 0 and len(store) == 0
        assert store.search(np.ones((1, 2)), 3) == []

    def test_corrupt_vector_file_is_skipped(self, tmp_path):
        store = build_store(tmp_path, {1: np.array([[1, 0]], np.float16), 2: np.array([[1, 0]], np.float16)})
        (tmp_path / "doc1" / "vectors" / "p0002.npy").write_bytes(b"garbage")
        assert store.load() == 1

    def test_wrong_dimensionality_is_skipped(self, tmp_path):
        store = build_store(tmp_path, {1: np.zeros((2, 3, 4), np.float16)})
        assert store.load() == 0

    def test_corrupt_manifest_is_skipped(self, tmp_path):
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "manifest.json").write_text("{nope")
        assert VisualStore(tmp_path).load() == 0

    @pytest.mark.parametrize("doc_id", ["../etc", "..", "a/b", "a\\b", "doc1/../../x", "", "doc 1", "%2e%2e"])
    def test_image_path_refuses_traversal(self, tmp_path, doc_id):
        store = build_store(tmp_path, {1: np.array([[1, 0]], np.float16)})
        assert store.image_path(doc_id, 1) is None

    def test_image_path_ok_and_missing_page(self, tmp_path):
        store = build_store(tmp_path, {1: np.array([[1, 0]], np.float16)})
        assert store.image_path("doc1", 1).name == "p0001.jpg"
        assert store.image_path("doc1", 2) is None
        assert store.image_path("doc1", 0) is None
        assert store.image_path("doc1", -3) is None


class TestCircuitBreaker:
    def test_opens_after_repeated_failures_and_stops_calling(self, monkeypatch):
        calls = []
        import httpx

        def boom(*a, **k):
            calls.append(1)
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx, "post", boom)
        client = VisualClient("http://127.0.0.1:9", timeout=0.1)
        for _ in range(6):
            assert client.embed_query("q") is None
        assert len(calls) == VisualClient.FAILURE_THRESHOLD and client.circuit_open

    def test_half_open_probe_after_cooldown_then_recovers(self, monkeypatch):
        import time
        import httpx

        client = VisualClient("http://x", timeout=0.1)
        client._failures, client._opened_at = 3, time.monotonic() - 999
        class R:
            def raise_for_status(self): pass
            def json(self): return {"vectors": [[1.0, 0.0]]}
        monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
        assert client.embed_query("q").shape == (1, 2) and client._failures == 0

    def test_retriever_never_raises_when_sidecar_is_down(self, tmp_path):
        store = build_store(tmp_path, {1: np.array([[1, 0]], np.float16)})
        store.load()
        client = VisualClient("http://127.0.0.1:9", timeout=0.2)
        assert VisualRetriever(store, client).search("anything") == []

    def test_pages_for_groups_by_doc(self):
        r = VisualRetriever(VisualStore("/nonexistent"), VisualClient("http://x"))
        assert r.pages_for([PageHit("a", 1, 1), PageHit("a", 4, 1), PageHit("b", 2, 1)]) == {"a": [1, 4], "b": [2]}
