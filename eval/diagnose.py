"""
Per-question diagnostics used to tune thresholds and fusion weights.

    python -m eval.diagnose

For every golden question prints the rank of the first unit containing the
answer under each ordering (dense / lexical / fused / reranked), plus the raw
confidence numbers that the abstention gate looks at.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Config  # noqa: E402
from eval.matching import contains_any  # noqa: E402
from eval.run_eval import load_golden  # noqa: E402
from rag.embedding import Embedder  # noqa: E402
from rag.engine import RagEngine, RetrievalOptions  # noqa: E402
from rag.reranker import load_reranker  # noqa: E402
from rag.store import Store  # noqa: E402


def rank_of(unit_ids, units, phrases):
    for i, uid in enumerate(unit_ids, 1):
        if uid in units and contains_any(units[uid].text, phrases):
            return i
    return None


def main() -> None:
    import os
    sweep_only = os.getenv("SWEEP_ONLY") == "1"
    Config.CONTEXT_CHAR_BUDGET = 10**9
    store = Store(Config.DATABASE_URL, Config.EMBED_DIM)
    engine = RagEngine(store, Embedder(), load_reranker(), None)
    golden = load_golden()
    rows, rerank_times = [], []
    print(f"{'id':<18}{'dense':>6}{'lex':>5}{'fused':>7}{'rerank':>8}   top_dense  top_rerank")
    for g in ([] if sweep_only else golden):
        res = {}
        for name, opt in {
            "dense": dict(use_dense=True, use_lexical=False, use_rerank=False),
            "lex": dict(use_dense=False, use_lexical=True, use_rerank=False),
            "fused": dict(use_dense=True, use_lexical=True, use_rerank=False),
            "rerank": dict(use_dense=True, use_lexical=True, use_rerank=True),
        }.items():
            t = time.perf_counter()
            r = engine.retrieve(g["q"], RetrievalOptions(use_visual=False, top_k=20, **opt))
            if name == "rerank":
                rerank_times.append(r.timings_ms.get("rerank", 0))
            res[name] = r
        units = {e.unit.unit_id: e.unit for r in res.values() for e in r.evidence}
        ranks = {}
        for name, r in res.items():
            ids = [e.unit.unit_id for e in r.evidence]
            ranks[name] = None if g.get("oos") else rank_of(ids, units, g["any_of"])
        row = (g["id"], ranks, res["dense"].top_dense, res["rerank"].top_rerank, g.get("oos", False))
        rows.append(row)
        f = lambda v: "-" if v is None else str(v)
        print(f"{g['id']:<18}{f(ranks['dense']):>6}{f(ranks['lex']):>5}{f(ranks['fused']):>7}{f(ranks['rerank']):>8}"
              f"   {res['dense'].top_dense:8.3f}  {('%.4f' % res['rerank'].top_rerank) if res['rerank'].top_rerank is not None else '-':>9}"
              f"{'   <- OOS' if g.get('oos') else ''}")

    ins = [r for r in rows if not r[4]]
    oos = [r for r in rows if r[4]]
    if not sweep_only:
        _summary(ins, oos, rerank_times)
    _sweep(engine, golden)


def _summary(ins, oos, rerank_times):
    print("\nanswerable: top_dense min/median", round(min(r[2] for r in ins), 3), round(statistics.median(r[2] for r in ins), 3))
    print("out-of-scope: top_dense max", round(max(r[2] for r in oos), 3))
    rr_in = [r[3] for r in ins if r[3] is not None]
    rr_out = [r[3] for r in oos if r[3] is not None]
    if rr_in and rr_out:
        print("answerable: top_rerank min", round(min(rr_in), 4), " out-of-scope: top_rerank max", round(max(rr_out), 4))
    print("rerank stage ms: median", round(statistics.median(rerank_times)), "max", round(max(rerank_times)))


def _sweep(engine, golden):
    print("\nlexical sweep (hybrid, no rerank, top_k=3):")
    answerable = [g for g in golden if not g.get("oos")]
    for df in (1.0, 0.08, 0.04, 0.02):
        for w in (0.0, 0.35, 0.7, 1.0):
            if df == 1.0 and w not in (0.0, 0.35, 1.0):
                continue
            hits, rr = 0, []
            for g in answerable:
                r = engine.retrieve(g["q"], RetrievalOptions(use_visual=False, use_rerank=False, top_k=20,
                                                             lexical_weight=w, lexical_max_df=df))
                ids = [e.unit for e in r.evidence]
                k = next((i for i, u in enumerate(ids, 1) if contains_any(u.text, g["any_of"])), None)
                hits += bool(k and k <= 3)
                rr.append(1 / k if k else 0)
            print(f"  max_df={df:<5} weight={w:<5} recall@3={hits}/{len(answerable)}  MRR={statistics.fmean(rr):.3f}")


if __name__ == "__main__":
    main()
