"""
The RAG engine: hybrid retrieval -> (visual fusion) -> rerank -> grounded,
streamed generation with citations and graceful degradation.

Pipeline (each stage can be toggled by RetrievalOptions - the eval harness uses
exactly this code to measure what each stage contributes):

    question
      ├─ dense   pgvector cosine over BGE embeddings           ┐
      ├─ lexical Postgres full-text (OR-query, ts_rank_cd)      ├─ RRF fusion
      └─ visual  ColPali-family MaxSim over page images ──────  ┘  (pages -> sections)
                       │
                cross-encoder rerank of ~20 candidate sections
                       │
          abstain if nothing is relevant, else top-k sections
          fitted whole into a character budget, numbered for citation
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from core.config import Config
from rag import guardrails
from rag.fusion import collapse_to_units, fit_to_budget, rrf_fuse
from rag.guardrails import SourceBlock
from rag.store import PassageHit, Store, UnitRow
from rag.streaming import ThinkSplitter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalOptions:
    use_dense: bool = True
    use_lexical: bool = True
    use_visual: bool = True
    use_rerank: bool = Config.RERANK_ENABLED
    top_k: int = Config.FINAL_TOP_K
    visual_weight: float = 0.6
    lexical_weight: float = Config.LEXICAL_WEIGHT
    lexical_max_df: float = Config.LEXICAL_MAX_DF


@dataclass
class Evidence:
    number: int
    unit: UnitRow
    score: float
    via: List[str]


@dataclass
class RetrievalResult:
    query: str
    evidence: List[Evidence] = field(default_factory=list)
    abstain: bool = False
    reason: str = ""
    truncated: bool = False
    visual_pages: List[Tuple[str, int, float]] = field(default_factory=list)
    timings_ms: Dict[str, float] = field(default_factory=dict)
    top_dense: float = 0.0
    top_rerank: Optional[float] = None
    degraded: List[str] = field(default_factory=list)

    def context_chars(self) -> int:
        return sum(len(e.unit.text) for e in self.evidence)


class RagEngine:
    def __init__(self, store: Store, embedder, reranker=None, visual=None, llm=None):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.visual = visual
        self._llm = llm
        self._cache: Dict[Tuple[str, RetrievalOptions], Tuple[float, RetrievalResult]] = {}
        self._cache_lock = threading.Lock()

    # ── retrieval ─────────────────────────────────────────────────────────
    def retrieve(self, query: str, options: Optional[RetrievalOptions] = None) -> RetrievalResult:
        options = options or RetrievalOptions()
        key = (query.casefold(), options)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        result = RetrievalResult(query=query)
        timings = result.timings_ms
        t0 = time.perf_counter()

        rankings: Dict[str, List[Any]] = {}
        passage_hits: Dict[int, PassageHit] = {}
        qvec = None

        failures: List[str] = []   # stages that raised; see the "everything failed" rule below

        if options.use_dense:
            try:
                t = time.perf_counter()
                qvec = self.embedder.embed_query(query)
                timings["embed"] = _ms(t)
                t = time.perf_counter()
                hits = self.store.dense_search(qvec, Config.DENSE_TOP_N)
                timings["dense"] = _ms(t)
                result.top_dense = hits[0].score if hits else 0.0
                passage_hits.update({h.passage_id: h for h in hits})
                rankings["dense"] = collapse_to_units([(h.passage_id, h.unit_id) for h in hits])
            except Exception as exc:
                logger.warning("dense retrieval failed: %s", exc)
                failures.append("dense")

        if options.use_lexical:
            try:
                t = time.perf_counter()
                hits = self.store.lexical_search(query, Config.LEXICAL_TOP_N, options.lexical_max_df)
                timings["lexical"] = _ms(t)
                passage_hits.update({h.passage_id: h for h in hits})
                rankings["lexical"] = collapse_to_units([(h.passage_id, h.unit_id) for h in hits])
            except Exception as exc:
                logger.warning("lexical retrieval failed: %s", exc)
                failures.append("lexical")

        if options.use_visual and self.visual is not None:
            t = time.perf_counter()
            page_hits = self.visual.search(query)
            timings["visual"] = _ms(t)
            if page_hits:
                result.visual_pages = [(h.doc_id, h.page, round(h.score, 3)) for h in page_hits]
                ordered: List[str] = []
                for doc_id, pages in self.visual.pages_for(page_hits).items():
                    ordered.extend(self.store.units_on_pages(doc_id, pages))
                rankings["visual"] = list(dict.fromkeys(ordered))
            elif Config.VISUAL_ENABLED and len(self.visual.store):
                result.degraded.append("visual")

        result.degraded.extend(failures)
        if not any(rankings.values()):
            if failures:
                # "No results" and "the search is broken" must not look the same to the
                # user: the first is an honest "not in the manual", the second is an outage.
                raise RuntimeError(f"all retrieval stages failed: {failures}")
            result.abstain, result.reason = True, "no_candidates"
            result.timings_ms["total"] = _ms(t0)
            return result

        fused = rrf_fuse(rankings, k=Config.RRF_K, weights={"visual": options.visual_weight, "lexical": options.lexical_weight})
        candidate_ids = [uid for uid, _ in fused[: Config.RERANK_TOP_N]]
        units = self.store.get_units(candidate_ids)
        candidate_ids = [u for u in candidate_ids if u in units]
        fused_score = dict(fused)
        membership = {
            name: set(ranking) for name, ranking in rankings.items()
        }

        scores: Dict[str, float] = {}
        if options.use_rerank and self.reranker is not None and candidate_ids:
            t = time.perf_counter()
            try:
                scores = self._rerank_units(query, candidate_ids, units, passage_hits)
            except Exception as exc:
                logger.warning("reranker failed (%s); using fused hybrid order", exc)
                scores = {}
                result.degraded.append("rerank")
            timings["rerank"] = _ms(t)
            result.top_rerank = max(scores.values()) if scores else None
        elif options.use_rerank and self.reranker is None:
            result.degraded.append("rerank")

        if scores:
            ordered_ids = sorted(candidate_ids, key=lambda u: -scores.get(u, 0.0))
            shown_score = scores
        else:
            ordered_ids = candidate_ids
            shown_score = fused_score

        # ── Abstention: don't answer from irrelevant evidence ───────────────
        # The dense cosine score is the calibrated gate: on the golden set every
        # answerable question scored >= 0.603 and every out-of-scope one <= 0.548
        # (python -m eval.diagnose). The reranker's probability is NOT used by
        # default - it could not separate the two groups (see docs).
        if "dense" in rankings and result.top_dense < Config.ABSTAIN_DENSE_THRESHOLD:
            result.abstain, result.reason = True, "low_dense_confidence"
        elif scores and result.top_rerank is not None and result.top_rerank < Config.ABSTAIN_RERANK_THRESHOLD:
            result.abstain, result.reason = True, "low_rerank_confidence"

        chosen_ids = ordered_ids[: options.top_k]
        texts = [units[u].text for u in chosen_ids]
        fitted, truncated = fit_to_budget(texts, Config.CONTEXT_CHAR_BUDGET)
        result.truncated = truncated
        for number, uid in enumerate(chosen_ids[: len(fitted)], start=1):
            unit = units[uid]
            if len(fitted[number - 1]) < len(unit.text):
                unit = replace(unit, text=fitted[number - 1])
            via = [name for name, members in membership.items() if uid in members]
            result.evidence.append(Evidence(number, unit, float(shown_score.get(uid, 0.0)), via))

        result.timings_ms["total"] = _ms(t0)
        if not result.abstain:
            self._cache_put(key, result)
        return result

    def _rerank_units(
        self,
        query: str,
        unit_ids: Sequence[str],
        units: Dict[str, UnitRow],
        passage_hits: Dict[int, PassageHit],
    ) -> Dict[str, float]:
        """
        Cross-encoders cap at 512 tokens, so scoring a 4,000-character section
        would silently ignore its tail. Instead score the section's best
        *matching passages* (what dense/lexical actually hit) and take the max;
        sections reached only visually are scored on their opening text.
        """
        by_unit: Dict[str, List[int]] = {}
        for pid, hit in passage_hits.items():
            by_unit.setdefault(hit.unit_id, []).append(pid)
        wanted = {
            u: sorted(by_unit.get(u, []), key=lambda p: -passage_hits[p].score)[: max(1, Config.RERANK_PASSAGES_PER_UNIT)] for u in unit_ids
        }
        ptexts = self.store.passages_text([p for ids in wanted.values() for p in ids])

        pairs: List[Tuple[str, str]] = []
        for uid in unit_ids:
            texts = [ptexts[p] for p in wanted[uid] if p in ptexts]
            if not texts:
                unit = units[uid]
                texts = [f"{unit.heading}\n{unit.text[:1500]}"]
            pairs.extend((uid, t) for t in texts)

        ranked = self.reranker.rerank(query, [(f"{i}", t) for i, (_, t) in enumerate(pairs)])
        best: Dict[str, float] = {}
        for idx, prob in ranked:
            uid = pairs[int(idx)][0]
            best[uid] = max(best.get(uid, 0.0), prob)
        return best

    # ── generation ────────────────────────────────────────────────────────
    def _get_llm(self):
        if self._llm is None:
            from langchain_groq import ChatGroq

            self._llm = ChatGroq(
                model=Config.GENERATION_MODEL,
                groq_api_key=Config.GROQ_API_KEY,
                temperature=0.1,
                max_tokens=Config.GENERATION_MAX_TOKENS,
                timeout=Config.GENERATION_TIMEOUT_S,
                max_retries=1,
            )
        return self._llm

    def _condense(self, query: str, history: Sequence[Tuple[str, str]]) -> str:
        """Rewrite a follow-up ("what about part-timers?") into a standalone question."""
        if not history:
            return query
        convo = "\n".join(f"{role}: {guardrails.neutralise_markup(msg)[:400]}" for role, msg in history)
        prompt = (
            "Rewrite the final user question so it is fully self-contained, using the conversation "
            "for context. Output ONLY the rewritten question, nothing else.\n\n"
            f"Conversation:\n{convo}\n\nFinal question: {query}\n\nStandalone question:"
        )
        try:
            out = self._get_llm().invoke(prompt).content.strip()
            out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().splitlines()[-1].strip()
            return guardrails.clean_query(out, Config.MAX_QUERY_CHARS)
        except Exception as exc:
            logger.warning("query condensation failed (%s); using the raw question", exc)
            return query

    def stream_answer(
        self, query: str, history: Optional[Sequence[Tuple[str, str]]] = None
    ) -> Iterator[Dict[str, Any]]:
        """Yield UI events: status, sources, thinking, answer, notice, done."""
        started = time.perf_counter()
        try:
            query = guardrails.clean_query(query, Config.MAX_QUERY_CHARS)
        except guardrails.QueryRejected as rejected:
            yield {"type": "error", "code": rejected.code, "content": rejected.reason}
            return

        signals = guardrails.injection_signals(query)
        if signals:
            logger.warning("possible prompt injection (%d pattern(s)) in query", len(signals))

        history = list(history or [])[-Config.MAX_HISTORY_TURNS * 2 :]
        yield {"type": "status", "content": "Searching the policy manual…"}
        standalone = self._condense(query, history) if history else query

        try:
            result = self.retrieve(standalone)
        except Exception as exc:
            logger.exception("retrieval failed")
            yield {"type": "error", "code": "retrieval_failed",
                   "content": "The knowledge base is unavailable right now. Please try again shortly."}
            return

        yield {"type": "sources", "sources": self._source_payload(result), "meta": self._meta(result)}

        if result.abstain or not result.evidence:
            yield {"type": "thinking_done"}
            yield {"type": "answer", "content": guardrails.ABSTAIN_MESSAGE}
            yield {"type": "done", "abstained": True, "ms": _ms(started)}
            return

        blocks = [
            SourceBlock(e.number, e.unit.citation_label(), e.unit.text) for e in result.evidence
        ]
        # The standalone question already carries any conversational context.
        prompt = guardrails.build_user_prompt(standalone, blocks)
        messages = [("system", guardrails.SYSTEM_PROMPT), ("human", prompt)]

        splitter = ThinkSplitter()
        answer_parts: List[str] = []
        emitted_any = False
        thinking_closed = False
        try:
            def pieces() -> Iterator[Tuple[str, str]]:
                for chunk in self._stream_with_retry(messages):
                    yield from splitter.feed(chunk)
                yield from splitter.flush()

            for kind, text in pieces():
                emitted_any = True
                if kind == "answer":
                    if not thinking_closed:
                        thinking_closed = True
                        yield {"type": "thinking_done"}
                    answer_parts.append(text)
                yield {"type": kind, "content": text}
        except Exception as exc:
            logger.warning("generation failed: %s", exc)
            if emitted_any:
                yield {"type": "notice", "content": "The AI service was interrupted mid-answer."}
            if not thinking_closed:
                yield {"type": "thinking_done"}
            yield {"type": "answer", "content": self._extractive_fallback(result)}
            yield {"type": "done", "degraded": True, "ms": _ms(started)}
            return

        if not thinking_closed:
            yield {"type": "thinking_done"}
        bad = _invalid_citations("".join(answer_parts), len(result.evidence))
        if bad:
            logger.warning("model cited non-existent source numbers: %s", bad)
        yield {"type": "done", "ms": _ms(started), "invalid_citations": bad}

    def _stream_with_retry(self, messages) -> Iterator[str]:
        """Retry only *before* the first token; a half-delivered answer is never replayed."""
        last: Optional[Exception] = None
        for attempt in range(2):
            started = False
            try:
                for piece in self._get_llm().stream(messages):
                    text = piece.content if isinstance(piece.content, str) else ""
                    if text:
                        started = True
                        yield text
                return
            except Exception as exc:
                last = exc
                if started:
                    raise
                time.sleep(0.8 * (attempt + 1))
        raise last if last else RuntimeError("generation failed")

    @staticmethod
    def _extractive_fallback(result: RetrievalResult) -> str:
        lines = ["The AI summariser is unavailable, so here are the most relevant passages from the policy manual:\n"]
        for e in result.evidence:
            snippet = re.sub(r"\s+", " ", e.unit.text).strip()[:600]
            lines.append(f"**[{e.number}] {e.unit.citation_label()}**\n{snippet}…\n")
        return "\n".join(lines)

    @staticmethod
    def _source_payload(result: RetrievalResult) -> List[Dict[str, Any]]:
        payload = []
        if result.abstain:
            return payload  # nothing relevant was found; don't present weak matches as sources
        for e in result.evidence:
            u = e.unit
            payload.append(
                {
                    "n": e.number,
                    "label": u.citation_label(),
                    "doc": u.doc_title,
                    "doc_id": u.doc_id,
                    "page": u.page_start,
                    "page_end": u.page_end,
                    "heading": u.heading,
                    "score": round(e.score, 3),
                    "via": e.via,
                    "snippet": re.sub(r"\s+", " ", u.text).strip()[:240],
                }
            )
        return payload

    @staticmethod
    def _meta(result: RetrievalResult) -> Dict[str, Any]:
        return {
            "timings_ms": {k: round(v, 1) for k, v in result.timings_ms.items()},
            "abstain": result.abstain,
            "reason": result.reason,
            "degraded": result.degraded,
            "visual_pages": result.visual_pages[:3],
        }

    # ── cache ─────────────────────────────────────────────────────────────
    def _cache_get(self, key):
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < Config.RETRIEVAL_CACHE_TTL_S:
                return hit[1]
            self._cache.pop(key, None)
        return None

    def _cache_put(self, key, result):
        with self._cache_lock:
            if len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), result)

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()


def _ms(since: float) -> float:
    return (time.perf_counter() - since) * 1000.0


_CITE = re.compile(r"\[(\d{1,2})\]")


def _invalid_citations(answer: str, n_sources: int) -> List[int]:
    return sorted({int(m) for m in _CITE.findall(answer) if int(m) < 1 or int(m) > n_sources})
