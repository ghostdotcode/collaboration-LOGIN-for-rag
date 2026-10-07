"""
Visual (ColPali-family) retrieval - the application side.

The vision model itself lives in a separate process (`visual_service/`) because
colpali-engine needs a newer `transformers` than the text stack pins, and a
multi-hundred-MB model should not share a process with the web API. This
module holds what the API needs: the on-disk page store, MaxSim scoring, and a
fault-tolerant HTTP client for embedding queries.

ColPali in one paragraph: each page *image* is embedded into ~1000 patch
vectors (128-d). A query is embedded into one vector per token. The page score
is late interaction ("MaxSim"): for every query-token vector take its best
match among the page's patch vectors, then sum. This keeps fine detail - a
table cell, a heading, a stamp - that a single pooled vector washes out.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from core.config import Config

logger = logging.getLogger(__name__)


def maxsim(query_vecs: np.ndarray, page_vecs: np.ndarray) -> float:
    """
    Late-interaction score of one page. Shapes: (nq, d) and (np, d).

    Returns 0.0 for empty input rather than raising, so a corrupt page file
    can never fail a user's question.
    """
    if query_vecs.size == 0 or page_vecs.size == 0:
        return 0.0
    similarities = query_vecs.astype(np.float32) @ page_vecs.astype(np.float32).T  # (nq, np)
    return float(similarities.max(axis=1).sum())


@dataclass
class PageHit:
    doc_id: str
    page: int
    score: float


class VisualStore:
    """Per-document page images + patch embeddings, memory-resident for search."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or Config.VISUAL_DIR)
        self._vectors: Dict[Tuple[str, int], np.ndarray] = {}
        self._lock = threading.Lock()
        self.loaded = False

    def doc_dir(self, doc_id: str) -> Path:
        return self.root / doc_id

    def load(self) -> int:
        """(Re)load every indexed document. Missing/corrupt files are skipped, not fatal."""
        vectors: Dict[Tuple[str, int], np.ndarray] = {}
        for manifest_path in self.root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("skipping unreadable visual manifest %s: %s", manifest_path, exc)
                continue
            doc_id = manifest.get("doc_id") or manifest_path.parent.name
            for entry in manifest.get("pages", []):
                try:
                    arr = np.load(manifest_path.parent / entry["vectors"])
                    if arr.ndim != 2:
                        raise ValueError(f"expected 2-D array, got {arr.shape}")
                    vectors[(doc_id, int(entry["page"]))] = arr
                except Exception as exc:
                    logger.warning("skipping page %s of %s: %s", entry.get("page"), doc_id, exc)
        with self._lock:
            self._vectors = vectors
            self.loaded = True
        return len(vectors)

    def __len__(self) -> int:
        return len(self._vectors)

    def search(self, query_vecs: np.ndarray, top_n: int) -> List[PageHit]:
        with self._lock:
            items = list(self._vectors.items())
        scored = [PageHit(doc, page, maxsim(query_vecs, arr)) for (doc, page), arr in items]
        scored.sort(key=lambda h: -h.score)
        return scored[:top_n]

    def image_path(self, doc_id: str, page: int) -> Optional[Path]:
        """Resolve a page image, refusing anything outside the store root."""
        if not doc_id.replace("-", "").replace("_", "").isalnum() or page < 1:
            return None
        candidate = (self.doc_dir(doc_id) / "pages" / f"p{page:04d}.jpg").resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


class VisualClient:
    """HTTP client with a circuit breaker so a dead sidecar costs ~nothing."""

    FAILURE_THRESHOLD = 3
    COOLDOWN_S = 30.0

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or Config.VISUAL_SERVICE_URL).rstrip("/")
        self.timeout = timeout or Config.VISUAL_TIMEOUT_S
        self._failures = 0
        self._opened_at = 0.0

    @property
    def circuit_open(self) -> bool:
        if self._failures < self.FAILURE_THRESHOLD:
            return False
        if time.monotonic() - self._opened_at > self.COOLDOWN_S:
            self._failures = self.FAILURE_THRESHOLD - 1  # half-open: allow one probe
            return False
        return True

    def embed_query(self, text: str) -> Optional[np.ndarray]:
        if self.circuit_open:
            return None
        import httpx

        try:
            response = httpx.post(f"{self.base_url}/embed_query", json={"text": text}, timeout=self.timeout)
            response.raise_for_status()
            arr = np.asarray(response.json()["vectors"], dtype=np.float32)
            self._failures = 0
            return arr
        except Exception as exc:
            self._failures += 1
            if self._failures >= self.FAILURE_THRESHOLD:
                self._opened_at = time.monotonic()
            logger.warning("visual embed failed (%s): %s", self._failures, exc)
            return None

    def healthy(self) -> bool:
        import httpx

        try:
            return httpx.get(f"{self.base_url}/health", timeout=2.0).status_code == 200
        except Exception:
            return False


class VisualRetriever:
    def __init__(self, store: Optional[VisualStore] = None, client: Optional[VisualClient] = None):
        self.store = store or VisualStore()
        self.client = client or VisualClient()

    def search(self, query: str, top_n: Optional[int] = None) -> List[PageHit]:
        """Never raises: visual evidence is a bonus, not a dependency."""
        if not Config.VISUAL_ENABLED or len(self.store) == 0:
            return []
        qvecs = self.client.embed_query(query)
        if qvecs is None:
            return []
        try:
            return self.store.search(qvecs, top_n or Config.VISUAL_TOP_N)
        except Exception as exc:  # pragma: no cover
            logger.warning("visual search failed: %s", exc)
            return []

    def pages_for(self, hits: Sequence[PageHit]) -> Dict[str, List[int]]:
        grouped: Dict[str, List[int]] = {}
        for hit in hits:
            grouped.setdefault(hit.doc_id, []).append(hit.page)
        return grouped
