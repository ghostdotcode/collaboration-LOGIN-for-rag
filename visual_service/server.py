"""
Visual embedding sidecar.

    .venv-visual/bin/uvicorn visual_service.server:app --port 8002

Endpoints
  GET  /health        200 once the model is loaded (503 while loading)
  POST /embed_query   {"text": "..."} -> {"vectors": [[...128 floats]...]}
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from visual_service import common

_ready = threading.Event()
_infer_lock = threading.Lock()  # one forward pass at a time on CPU


def _warm():
    common.load()
    common.embed_queries(["warm up"])
    _ready.set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(title="Meritech visual embedder", lifespan=lifespan)


class QueryIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.get("/health")
def health():
    if not _ready.is_set():
        raise HTTPException(503, "model loading")
    return {"status": "ok", "model": common.MODEL_NAME}


@app.post("/embed_query")
def embed_query(body: QueryIn):
    if not _ready.is_set():
        raise HTTPException(503, "model loading")
    started = time.perf_counter()
    with _infer_lock:
        (vectors,) = common.embed_queries([body.text])
    return {"vectors": vectors.tolist(), "ms": round((time.perf_counter() - started) * 1000, 1)}
