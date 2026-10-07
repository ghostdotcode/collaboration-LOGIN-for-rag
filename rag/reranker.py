"""Cross-encoder reranker with graceful degradation."""

from __future__ import annotations

import logging
import math
from typing import List, Optional, Sequence, Tuple

from core.config import Config

logger = logging.getLogger(__name__)


def sigmoid(x: float) -> float:
    # Guard against overflow for very negative / positive logits.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Reranker:
    """
    Scores (query, passage) *jointly*, which is far more accurate than comparing
    two independently computed embeddings - at the cost of one forward pass per
    candidate, so it is only run on the ~20 survivors of first-stage fusion.
    """

    def __init__(self, model_name: Optional[str] = None):
        from sentence_transformers import CrossEncoder

        self.model_name = model_name or Config.RERANK_MODEL
        self.model = CrossEncoder(self.model_name, device="cpu", max_length=Config.RERANK_MAX_LENGTH)

    def score(self, query: str, passages: Sequence[str]) -> List[float]:
        if not passages:
            return []
        import torch

        # CrossEncoder.predict() applies a Sigmoid by default for single-label models.
        # Squashing that *again* (as an earlier version here did) compresses every
        # score into [0.50, 0.73] and makes thresholds meaningless. Ask for raw logits.
        logits = self.model.predict(
            [(query, p) for p in passages], activation_fct=torch.nn.Identity(), show_progress_bar=False
        )
        return [sigmoid(float(x)) for x in logits]

    def rerank(self, query: str, items: Sequence[Tuple[str, str]]) -> List[Tuple[str, float]]:
        """items = [(id, text)]  ->  [(id, probability)] sorted best-first."""
        scores = self.score(query, [text for _, text in items])
        ranked = sorted(zip((i for i, _ in items), scores), key=lambda p: -p[1])
        return ranked


def load_reranker() -> Optional[Reranker]:
    """Never raises: a missing model must not take the whole assistant down."""
    if not Config.RERANK_ENABLED:
        logger.info("Reranker disabled by configuration.")
        return None
    try:
        return Reranker()
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Reranker unavailable (%s); falling back to fused ranking.", exc)
        return None
