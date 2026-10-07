"""Shared model loading for the visual service and the page indexer.

Runs inside `.venv-visual` (colpali-engine + a recent transformers), isolated
from the text-RAG environment on purpose.
"""

from __future__ import annotations

import os
import threading

import torch

MODEL_NAME = os.getenv("VISUAL_MODEL", "vidore/colSmol-256M")
# Page images are tiled by the SmolVLM processor; capping the longest edge
# bounds patch count (and therefore CPU time and storage) per page.
LONGEST_EDGE = int(os.getenv("VISUAL_LONGEST_EDGE", "1024"))

_lock = threading.Lock()
_cache: dict = {}


def load():
    """Return (model, processor), loading once."""
    with _lock:
        if "model" not in _cache:
            from colpali_engine.models import ColIdefics3, ColIdefics3Processor

            torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
            model = ColIdefics3.from_pretrained(
                MODEL_NAME, torch_dtype=torch.float32, device_map="cpu"
            ).eval()
            processor = ColIdefics3Processor.from_pretrained(MODEL_NAME)
            try:
                processor.image_processor.size = {"longest_edge": LONGEST_EDGE}
            except Exception:
                pass
            _cache["model"], _cache["processor"] = model, processor
        return _cache["model"], _cache["processor"]


def embed_queries(texts):
    model, processor = load()
    batch = processor.process_queries(list(texts)).to(model.device)
    with torch.no_grad():
        out = model(**batch)  # (B, n_tokens, 128)
    return [row[mask.bool()].float().cpu().numpy() for row, mask in zip(out, batch["attention_mask"])]


def embed_images(images):
    model, processor = load()
    batch = processor.process_images(list(images)).to(model.device)
    with torch.no_grad():
        out = model(**batch)
    return [row[mask.bool()].float().cpu().numpy() for row, mask in zip(out, batch["attention_mask"])]
