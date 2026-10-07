"""
Retrieval evaluation harness.

    python -m eval.run_eval                 # all modes
    python -m eval.run_eval --modes legacy hybrid_rerank

A question counts as a HIT when the retrieved context contains one of its
`any_of` answer phrases. That is deliberately independent of how the corpus was
chunked, so the legacy pipeline and the new one are scored on equal terms.

Out-of-scope questions measure the other half of reliability: the system
should ABSTAIN rather than answer from irrelevant policy text.

This measures *retrieval* (does the evidence contain the answer?). It does not
call the LLM, so it is free, deterministic and fast enough to run on every
change.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Config  # noqa: E402
from eval.matching import contains_any  # noqa: E402

GOLDEN = Path(__file__).with_name("golden.jsonl")
RESULTS = Path(__file__).with_name("results")

MODES = {
    "legacy": None,
    "dense": dict(use_dense=True, use_lexical=False, use_visual=False, use_rerank=False),
    "lexical": dict(use_dense=False, use_lexical=True, use_visual=False, use_rerank=False),
    "hybrid": dict(use_dense=True, use_lexical=True, use_visual=False, use_rerank=False),
    "visual": dict(use_dense=False, use_lexical=False, use_visual=True, use_rerank=False),
    "hybrid_visual": dict(use_dense=True, use_lexical=True, use_visual=True, use_rerank=False),
    "hybrid_rerank": dict(use_dense=True, use_lexical=True, use_visual=False, use_rerank=True),
    "full": dict(use_dense=True, use_lexical=True, use_visual=True, use_rerank=True),
}


def load_golden(path: Path = GOLDEN) -> List[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class LegacyRunner:
    """The ORIGINAL retriever's retrieval logic, byte for byte: MMR k=2 over the
    old `meritech_policy` collection, expanded to parent text (18k char cap)."""

    def __init__(self):
        from langchain_community.vectorstores import PGVector
        from langchain_huggingface import HuggingFaceEmbeddings

        url = Config.DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
        self.store = PGVector(
            collection_name="meritech_policy",
            connection_string=url,
            embedding_function=HuggingFaceEmbeddings(
                model_name="BAAI/bge-large-en-v1.5", model_kwargs={"device": "cpu"}
            ),
        )

    def run(self, question: str) -> dict:
        t = time.perf_counter()
        docs = self.store.max_marginal_relevance_search(question, k=2, fetch_k=10)
        parents: Dict[str, str] = {}
        for d in docs:
            parents.setdefault(d.metadata.get("parent_id", "?"), d.metadata.get("parent_content", d.page_content))
        context = "\n\n---\n\n".join(parents.values())[:18000]
        return {"context": context, "abstain": False, "ms": (time.perf_counter() - t) * 1000,
                "first_hit_rank": None, "units": list(parents.values())}


class EngineRunner:
    def __init__(self, engine, options):
        self.engine, self.options = engine, options

    def run(self, question: str) -> dict:
        r = self.engine.retrieve(question, self.options)
        units = [e.unit.text for e in r.evidence]
        return {"context": "\n\n".join(units), "abstain": r.abstain, "ms": r.timings_ms.get("total", 0.0),
                "units": units}


def first_rank(units: List[str], phrases) -> int | None:
    for i, text in enumerate(units, start=1):
        if contains_any(text, phrases):
            return i
    return None


def evaluate(mode: str, runner, golden: List[dict]) -> dict:
    rows, by_kind = [], defaultdict(list)
    latencies, ctx_sizes = [], []
    oos_total = oos_ok = 0
    for g in golden:
        out = runner.run(g["q"])
        latencies.append(out["ms"])
        if g.get("oos"):
            oos_total += 1
            ok = bool(out["abstain"])
            oos_ok += ok
            rows.append({"id": g["id"], "kind": g["kind"], "ok": ok, "abstained": out["abstain"]})
            continue
        hit = contains_any(out["context"], g["any_of"])
        rank = first_rank(out["units"], g["any_of"])
        # A system that abstains on an answerable question is a miss, not a pass.
        if out["abstain"]:
            hit, rank = False, None
        ctx_sizes.append(len(out["context"]))
        rows.append({"id": g["id"], "kind": g["kind"], "ok": hit, "rank": rank, "abstained": out["abstain"]})
        by_kind[g["kind"]].append(hit)

    answerable = [r for r in rows if "rank" in r]
    hits = sum(r["ok"] for r in answerable)
    mrr = statistics.fmean((1 / r["rank"]) if r.get("rank") else 0.0 for r in answerable) if answerable else 0.0
    return {
        "mode": mode,
        "recall": hits / len(answerable) if answerable else 0.0,
        "hits": hits,
        "answerable": len(answerable),
        "mrr": mrr,
        "oos_abstain": (oos_ok / oos_total) if oos_total else None,
        "oos_ok": oos_ok,
        "oos_total": oos_total,
        "avg_context_chars": int(statistics.fmean(ctx_sizes)) if ctx_sizes else 0,
        "p50_ms": statistics.median(latencies),
        "p95_ms": sorted(latencies)[int(0.95 * (len(latencies) - 1))],
        "by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in by_kind.items()},
        "misses": [r["id"] for r in answerable if not r["ok"]],
        "false_answers": [r["id"] for r in rows if r["kind"] in ("out-of-scope", "injection") and not r["ok"]],
        "rows": rows,
    }


def print_table(results: List[dict]) -> None:
    head = f"{'mode':<22}{'recall':>8}{'MRR':>7}{'OOS abstain':>13}{'ctx chars':>11}{'p50 ms':>9}{'p95 ms':>9}"
    print(head), print("-" * len(head))
    for r in results:
        oos = "n/a" if r["oos_abstain"] is None else f"{r['oos_ok']}/{r['oos_total']}"
        print(f"{r['mode']:<22}{r['hits']:>3}/{r['answerable']:<4}{r['mrr']:>7.3f}{oos:>13}"
              f"{r['avg_context_chars']:>11}{r['p50_ms']:>9.0f}{r['p95_ms']:>9.0f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    ap.add_argument("--golden", type=Path, default=GOLDEN, help="question file (eval/heldout.jsonl = unseen during tuning)")
    ap.add_argument("--out", type=Path, default=RESULTS / "latest.json")
    args = ap.parse_args()

    golden = load_golden(args.golden)
    print(f"{len(golden)} questions ({sum(1 for g in golden if not g.get('oos'))} answerable, "
          f"{sum(1 for g in golden if g.get('oos'))} out-of-scope)\n")

    engine = None
    results = []
    for mode in args.modes:
        if mode == "legacy":
            runner = LegacyRunner()
        else:
            if engine is None:
                needs_rerank = any(MODES[m] and MODES[m]["use_rerank"] for m in args.modes)
                from rag.embedding import Embedder
                from rag.engine import RagEngine
                from rag.reranker import Reranker
                from rag.store import Store
                from rag.visual import VisualRetriever

                visual = VisualRetriever()
                visual.store.load()
                engine = RagEngine(Store(Config.DATABASE_URL, Config.EMBED_DIM), Embedder(),
                                   Reranker() if needs_rerank else None, visual)
            from rag.engine import RetrievalOptions

            runner = EngineRunner(engine, RetrievalOptions(**MODES[mode]))
        print(f"running {mode} ...", flush=True)
        results.append(evaluate(mode, runner, golden))

    print()
    print_table(results)
    for r in results:
        print(f"\n[{r['mode']}] by kind: {r['by_kind']}")
        if r["misses"]:
            print(f"[{r['mode']}] missed: {', '.join(r['misses'])}")
        if r["false_answers"]:
            print(f"[{r['mode']}] answered out-of-scope: {', '.join(r['false_answers'])}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nsaved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
