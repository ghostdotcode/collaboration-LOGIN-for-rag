"""Embedding wrapper: batching, normalisation and the BGE query instruction."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import List, Sequence

from core.config import Config

# BGE v1.5 is trained so that *queries* carry this instruction and *passages*
# do not. The old code embedded raw questions, leaving measurable retrieval
# quality on the table for every short query.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class Embedder:
    def __init__(self, model_name: str | None = None, cache_size: int = 512):
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        self.model_name = model_name or Config.EMBED_MODEL
        self.model = SentenceTransformer(self.model_name, device="cpu")
        self.dim = self.model.get_sentence_embedding_dimension()
        self._cache: "OrderedDict[str, List[float]]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def embed_passages(self, texts: Sequence[str], batch_size: int = 32) -> List[List[float]]:
        vectors = self.model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def embed_query(self, query: str) -> List[float]:
        with self._lock:
            hit = self._cache.get(query)
            if hit is not None:
                self._cache.move_to_end(query)
                return hit
        vector = self.model.encode(
            BGE_QUERY_INSTRUCTION + query, normalize_embeddings=True, show_progress_bar=False
        ).tolist()
        with self._lock:
            self._cache[query] = vector
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return vector
