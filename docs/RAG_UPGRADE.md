# RAG Upgrade: audit, redesign, measurements, edge cases

This document records (1) what the chatbot looked like **before**, (2) what each part was
**changed to** and **why that is better**, (3) what was **added that did not exist at all**,
(4) the **edge cases tested** and how they came out, and (5) what is still **not done** or
**not proven**. Every number below comes from a command that was actually run on this
machine (8 CPUs, no GPU); commands are given so you can re-run them.

> **Read this first: three things I want to be upfront about**
>
> 1. **"OKF" was never defined.** You chose "Open Knowledge Format / a structured
>    knowledge-base format (I'll describe it)" and did not describe it afterwards. I
>    implemented OKF as a small, versioned, provenance-carrying record format
>    (`rag/okf.py`, section 3.2). If you meant a specific existing standard, tell me its
>    name or give me its spec and I'll adapt the module; the rest of the system only
>    depends on the `KnowledgeDocument`/`KnowledgeUnit` classes.
> 2. **The old retriever was already good at plain recall** on this document: it found the
>    answer for 24 of 25 questions. The measurable wins are elsewhere (refusing off-topic
>    questions, citations with page numbers, streaming, security, tests). Two of the
>    "advanced" stages I built (cross-encoder reranking and ColPali visual retrieval)
>    **did not help on this corpus and are switched off by default.** They are working,
>    tested and one environment variable away. Section 5 has the numbers.
> 3. **The evaluation sets are small and I wrote them.** 25 answerable + 6 out-of-scope
>    questions for tuning, plus 6 + 8 unseen ones for a held-out check. That is enough to
>    catch regressions and to rank options that differ a lot, not to claim statistical
>    significance on differences of one question.

---

## 1. How to run and test it

```bash
scripts/run_all.sh          # starts Postgres+pgvector (:5433), Redis (:6380), LMS API (:8001), chatbot (:8000)
scripts/run_all.sh stop
```

Open **http://localhost:8000** (it redirects to the login page).

* Sign up with any email. Because LMS auto-provisioning is switched on by the script
  (`SSO_AUTO_PROVISION=true`), the **Apply for Leave** button works for any new account
  and the new employee gets leave balances immediately. (Before, a new sign-up hit
  "ask HR to add you".)
* Ask: *"How many days of annual leave am I entitled to?"*, then *"and what about
  fathers?"* (follow-up), then *"What is the capital of France?"* (it should decline).
* Click a citation chip such as `[1] HR Policy Manual, p. 27 (…)` to see the real PDF page.
* Models load in the background after boot (~15 s); the status dot turns green when ready.
  `curl localhost:8000/api/status` shows per-component state.

Test suites (all currently green). Prerequisites: the docker containers from
`scripts/run_all.sh` are up, and two throwaway databases exist (`docker exec meritech-db-1
psql -U postgres -c "CREATE DATABASE rag_test"` then `... -d rag_test -c "CREATE EXTENSION vector"`,
and `CREATE DATABASE lms_test`). The LMS suite **drops its tables on teardown**, so never
point `LMS_TEST_DATABASE_URL` at a real database. For `eval.*` scripts run from the repo
root with `DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/meritech_db`
and `venv/bin/python -m eval.run_eval`.

| Suite | Command | Result |
|---|---|---|
| Chatbot / RAG (unit, API, DB integration) | `venv/bin/python -m pytest tests` | **245 passed** |
| LMS backend | `cd lms_backend && LMS_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/lms_test ../venv/bin/python -m pytest tests` | **132 passed, 2 skipped** (the 2 are browser e2e tests that need Playwright; never run) |
| Retrieval evaluation | `python -m eval.run_eval` (tuning set) / `--golden eval/heldout.jsonl` | section 5 |
| Threshold/weight diagnostics | `python -m eval.diagnose` | section 5.3 |

---

## 2. Gap audit: before → after → why it is better

### 2.1 Retrieval and content handling (the "AI" side)

Measured facts about the **old** index (`data/processed/final_enriched_chunks.json`,
587 child chunks under 199 parents):

* The text that was embedded was **2.69× larger than the actual content**: an LLM-written
  `[Context: …]` header was prepended to every child. The header was asked to be
  "one sentence, max 15 words" but its **median length was 466 characters, and 542 of 587
  (92 %) exceeded 200**. The header was larger than the passage it described, so the
  embedding mostly encoded the header.
* **40 child chunks were under 60 characters** (minimum: 6) and **32 parents under 80
  characters** (minimum: 13), i.e. heading stubs like `# HUMAN RESOURCES POLICY MANUAL`.
* **No page numbers anywhere.** The parser flattened the PDF to one markdown string.
* Parent text was stored inside every child's metadata (stored once per child).

| # | Before | After | Why it is better |
|---|---|---|---|
| 1 | Embedded `[Context: <466-char LLM header>] + passage` | Embedded a **deterministic breadcrumb** (`Doc > Section > Subsection`) + passage; an LLM summary is optional and capped at one sentence ≤ 200 chars (`rag/okf.py: clean_summary`) | Free, always accurate, bounded size; gives both dense and keyword search the section vocabulary. No paid LLM call per section. |
| 2 | Stub chunks indexed | Quality gate `normalise_units`: drops heading stubs (<80 chars) and exact duplicates, merges tiny passages into a neighbour so no text is lost | Less retrieval noise. |
| 3 | No pages | Every section carries `page_start/page_end`. Existing data recovered by 3-gram overlap against the PDF (`rag/pages.py`): **167 / 167 sections located** | Citations with page numbers; page previews; enables visual retrieval. |
| 4 | Raw question embedded | BGE **query instruction** prefix (what the model was trained with) | Better matching for short queries; cached (LRU). |
| 5 | MMR, k=2 child chunks, then parent expansion | Dense (pgvector HNSW) + keyword (Postgres full-text) searched in parallel, fused by Reciprocal Rank Fusion, top-3 **whole sections** | MMR optimises *diversity*, which is the wrong objective for a factual lookup; k=2 gave no margin for error. |
| 6 | Naive keyword OR-matching would have drowned the ranking (first attempt, see 5.3) | Keyword stage only uses **distinctive terms**: stemmed query words found in ≤ 8 % of passages, weighted 0.25 in the fusion | Keyword search is kept as insurance for exact tokens (acronyms, form names) without letting common words like "leave"/"days" override meaning. |
| 7 | Joined context sliced at 18,000 chars, which can cut a section mid-sentence | Whole sections fitted into a budget (`fit_to_budget`); average context is ~4 k chars | The model never receives half a rule. |
| 8 | **Always answered**, even to "what is the capital of France" (it received irrelevant policy text and the model improvised) | **Abstains** when the best dense similarity is below 0.575 and says so (no sources shown) | Off-topic questions: **0 % → 100 %** correctly declined (section 5). |
| 9 | Follow-ups ("and for fathers?") were embedded alone | Follow-ups are rewritten into a standalone question using the last turns (falls back to the raw question if that LLM call fails) | Multi-turn conversations work (verified live). |
| 10 | Retrieved text and question pasted straight into the prompt | `<context>` / `<question>` fenced and declared untrusted data; forged `</context>`, `<system>`, `<think>` tags in documents or questions are stripped; question normalised (NFKC, zero-width and bidi characters removed) | A document or user can't close the fence or impersonate system text. Injection attempts are also logged. |
| 11 | The whole completion was generated, then regex-split, then re-sent word by word with `sleep(0.015)` (**fake typing; first visible token only after the full generation**) | **True token streaming** with a tag parser that survives `<think>` split across chunks (`rag/streaming.py`) | First token in ~0.5-2.8 s in the live test instead of waiting for the full answer. |
| 12 | One LLM call, no timeout/retry/fallback | Timeout, one retry **only before the first token** (a half-delivered answer is never replayed), mid-stream drop keeps the partial and adds a notice, LLM down → **extractive fallback** that quotes the matched sections | The assistant degrades instead of dying. |
| 13 | No citations | Sources numbered `[1] [2]`, validated: citations of non-existent numbers are detected and logged | The user can verify each claim; hallucinated references are caught. |
| 14 | Ingestion = scripts named like tests (`test_ingestion.py`, `fast_index.py`) that re-run every step, including paid LLM enrichment and coreference rewriting of legal text | `ingestion/pipeline.py`: incremental (skip if file hash, OKF version and embedding model are unchanged), **atomic replace** in one transaction, coref and enrichment are **opt-in** | Re-running costs nothing when nothing changed; a failed run leaves the old data intact (tested). |
| 15 | Third-party LangChain `PGVector` table, no keyword index, no HNSW | Own schema `okf_documents / okf_units / okf_passages` with an **HNSW** vector index and a **GIN** full-text index; parent text stored once | Hybrid search in one database; faster; no duplication. |

### 2.2 Application, security and operations

| # | Before | After |
|---|---|---|
| 16 | **`/api/ask` required no login**: anyone who could reach port 8000 could spend your Groq quota | Requires a valid token; **20 requests/min per user** (429 with `Retry-After`) |
| 17 | `CORS allow_origins=["*"]` together with `allow_credentials=True` | Explicit origin allow-list (`CORS_ORIGINS`); only needed methods/headers |
| 18 | 20-60 s model load at import time: the server accepted no connection and `/api/status` always said `ready: true` | Server is up immediately; models load in a background thread; `/api/status` reports real per-component state; `/healthz` for liveness |
| 19 | `marked.parse(llm_output)` assigned to `innerHTML` (**XSS from model output**), `marked` loaded from an **unpinned CDN** | Output sanitised with DOMPurify; marked 12.0.2 and DOMPurify 3.1.6 vendored locally (`frontend/vendor/`) |
| 20 | Missing token returned 403; JWT secret silently fell back to a hard-coded dev value even in production; tokens had no `iat` | 401 + `WWW-Authenticate`; **refuses to boot in production** with the default/short secret; `iat` added |
| 21 | Login/sign-up: case-sensitive emails (`Alice@x.com` ≠ `alice@x.com`), password >72 bytes → unhandled 500 (bcrypt 5), email >120 chars → DB error 500, duplicate-sign-up race → 500, validation errors rendered as `[object Object]` | Emails folded to lowercase (lookup also tolerant of old mixed-case rows); 72-byte check in the schema; length checks; `IntegrityError` handled; all 422s flattened to one readable sentence |
| 22 | Unlimited password guessing; unknown-email logins were faster than wrong-password ones | 10 attempts/min/IP then 429; a dummy bcrypt verify equalises timing (tested) |
| 23 | `/docs` always public; no security headers | `/docs` disabled in production; `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, request id |
| 24 | `requirements.txt` missed `SQLAlchemy`, `PyJWT`, `bcrypt`, `email-validator`, `httpx` (they were imported anyway) | Declared |
| 25 | LMS: a chatbot user without an LMS profile was turned away | **JIT provisioning** (opt-in `SSO_AUTO_PROVISION`): EMPLOYEE role only (token claims can never grant a role), seat limit enforced, optional email-domain allow-list, audit record, balances initialised immediately |
| 26 | LMS test `test_retroactive_request_blocked_when_policy_forbids` **failed one day in seven** (it used "today − 10 days", which lands on a weekend and trips a different validation) | Picks a working day. (Found because the suite failed today.) |
| 27 | Section headings in citations contained PDF page numbers parsed as headings ("…> LEAVE > 27") | Display strips numeric-only headings and the document-title root; the ingester no longer produces them |

---

## 3. Things added that did not exist at all

1. **OKF** (`rag/okf.py`). *Assumed meaning, see the note at the top.*
   `KnowledgeDocument → KnowledgeUnit (section the LLM reads) → passages (spans that are
   embedded)`. Content-hash ids (idempotent), page spans, heading path, flags, versioned
   JSONL (`data/okf/*.okf.jsonl`, a `_okf` header line + one unit per line; a file with an
   unknown major version or a corrupt line is rejected, not half-loaded).
2. **Hybrid retrieval + RRF + abstention + whole-unit budgeting** (`rag/engine.py`,
   `rag/fusion.py`). Each stage can fail independently and the others continue
   (`degraded` list); only "every stage failed" is reported as an outage, so "not in the
   manual" and "the search is broken" never look the same.
3. **Evaluation harness** (`eval/`). `golden.jsonl` (25 + 6), `heldout.jsonl` (6 + 8),
   `run_eval.py` (compares the *old retriever, verbatim* with every new stage: recall,
   MRR, off-topic abstention, context size, latency), `diagnose.py` (per-question ranks,
   threshold and weight sweeps). Every gold answer phrase was checked to exist in the
   source PDF text.
4. **ColPali-family visual retrieval** (`rag/visual.py`, `visual_service/`): page images
   embedded with `vidore/colSmol-256M` (CPU-viable), late-interaction MaxSim search,
   a separate-process sidecar (`:8002`) because `colpali-engine` needs
   `transformers 5.x` while the text stack is pinned to `4.38.2`, a circuit breaker
   (3 failures → 30 s cool-down), resumable page indexer (75/75 pages indexed,
   345 patches/page, ~15 s/page on this CPU, ~0.5 s per query). **Off by default.**
5. **Citation page previews**: `/api/pages/{doc}/{page}` renders the real PDF page on
   demand (authenticated, cached, document looked up by id in the database so no path
   from the client is ever used).
6. Conversation memory in the UI (last turns sent back), session-expiry handling,
   429/401 messages, `notice` events.
7. `/healthz`, `/api/status` component detail, request ids, `scripts/run_all.sh`.
8. A real automated test suite for the chatbot (it had none; section 4).

---

## 4. Edge cases tested

All of these are automated tests unless marked "live".

**Question input** (`test_guardrails.py`): empty, whitespace-only, zero-width-only,
symbols-only (`?!?!`), emoji-only, `None`/number/list instead of text, exactly 1000 vs
1001 characters, control characters (NUL, BEL; newline preserved), right-to-left override
characters, full-width lookalikes (`ＩＧＮＯＲＥ`→`IGNORE`), non-English text (Swahili,
Japanese) accepted.

**Prompt injection**: 7 attack phrasings are flagged; 3 ordinary questions that contain
words like "ignore"/"previous" are **not** flagged; 9 ways of forging prompt delimiters
(`</CONTEXT>`, `< / context >`, `<think>`, `<system>`, `<source n="9">`…) are stripped from
documents and questions; the prompt keeps exactly one `<context>` pair; a hostile source
label cannot break out of its attribute (live: the injection question was declined).

**Streaming parser** (`test_streaming.py`): the `<think>…</think>` tags split at *every
possible pair of cut positions*, one character at a time, partial tag that turns out to be
plain text (`<thi` + `s is fine`), a literal `<` in an answer, unclosed think block, several
blocks, empty block, empty stream, a stray `</think>` (hidden from users).

**Retrieval engine with fakes** (`test_engine.py`): dense-only and keyword-only hits both
surface; each stage failing alone (database error, embedder crash) still answers from the
other; all stages failing → clean error that does **not leak the internal message**;
reranker crash → falls back; abstain on irrelevant evidence and on no candidates;
abstentions are not cached; cache is case-insensitive and expires; context budget smaller
than the first section truncates but never returns nothing; visual search down → answers
and reports `degraded`; LLM down → extractive fallback; one transient LLM failure →
retried once; **mid-stream drop → partial kept, not replayed**; hallucinated citation
`[7]` detected; follow-up condensed; condensation failure → raw question used; **24
simultaneous questions → identical answers, no errors**.

**Database and ingestion** (`test_store_and_ingest.py`, real Postgres): idempotent
schema creation; unchanged file skipped; changed file replaced without duplicates; forced
reindex; embedding-model change triggers reindex; **a failed re-ingest (wrong vector size
mid-transaction) leaves the old data fully intact**; **an empty/scanned PDF can never wipe
an existing knowledge base**; corrupt PDF rejected; delete cascades; two documents are
isolated; same content → same ids; keyword search against hostile input (SQL
`'; DROP TABLE…`, tsquery operators `& ! ( ) : * <->`, backslash, unterminated quote, NUL
byte, only stop-words, CJK, empty, 5,000 characters) never raises and leaves the table
intact.

**Visual store** (`test_visual.py`): corrupt `.npy` skipped, wrong dimensionality skipped,
corrupt manifest skipped, empty store, float16 vectors, empty query/page vectors score 0
(never crash), **8 path-traversal spellings** (`../etc`, `a/b`, `%2e%2e`, …) refused, page 0
and negative pages refused, circuit breaker opens after 3 failures and stops calling the
sidecar, half-open probe recovers, sidecar down → search returns `[]`.

**HTTP/auth** (`test_api.py`, real database): sign-up with weak/mismatched/over-long
(73 bytes; 40 two-byte characters = 80 bytes) passwords, bad emails, blank names, over-long
email (120-char column limit), SQL-injection text in a name (inert), unicode names and password,
malformed JSON/wrong types, duplicate email in a different case, stored lowercase;
login with wrong password vs unknown user (identical response), 5,000-char password
(422, not 500), brute force → 429 on the 11th attempt, timing gap bounded; `/api/ask`
with no token, garbage token, wrong scheme, token signed with another secret, **expired**
token, **`alg: none`** token, token of a **deleted user** (all 401); bad bodies (missing
query, wrong type, 5,001 chars, `system` role in history, 13 history turns) → readable
422; engine boot failure and unexpected exceptions → clean error event; rate limit is
**per user** (a second user is unaffected); unicode survives SSE; CORS blocks an unknown
origin; static path traversal (`/frontend/../.env` and two encodings) → 400/404; page
endpoint requires auth.

**LMS SSO** (`test_sso_provisioning.py`): existing user; mixed-case token subject;
unknown user refused when the flag is off; provisioned (with audit record) when on;
second exchange doesn't duplicate; name fallback (claims → `name` → email local-part);
**token claims cannot grant a role**; domain allow-list; **seat limit → 402**;
deactivated users are *not* resurrected; malformed subjects; expired and forged tokens;
balances created at once.

**Live, against the running stack**: login → chat answer streamed with citations →
follow-up → off-topic declined → injection declined → empty question rejected → no-token
request 401 → citation chip opens the real PDF page (browser) → "Apply for Leave"
signs the new user into the LMS (greeting "Hi, E2E"). The temporary accounts used for this
were removed (the LMS rows are deactivated rather than deleted, because the audit log is
append-only by design).

---

## 5. Measured results

### 5.1 Retrieval quality (`python -m eval.run_eval`)

Tuning set: 25 answerable + 6 out-of-scope (off-topic or injection) questions.
"Recall" = the retrieved context contains the answer phrase.

| Mode | Recall | MRR | Off-topic declined | Avg context (chars) | p50 latency |
|---|---|---|---|---|---|
| **Old retriever (verbatim)** | 24/25 | 0.960 | **0/6** | 2,297 | 284 ms |
| New: dense only | 25/25 | 0.960 | 6/6 | 4,071 | 356 ms |
| New: keyword only | 19/25 | 0.600 | 2/6 | 3,779 | 13 ms |
| **New: hybrid (default)** | **25/25** | 0.933 | **6/6** | 3,971 | 22 ms* |
| + visual (ColSmol, CPU) | 25/25 | 0.893 | 6/6 | 3,826 | 973 ms |
| visual only | 4/25 | 0.053 | 0/6 | 2,772 | 617 ms |
| + cross-encoder rerank | 22/25 | 0.760 | 6/6 | 3,548 | 2,304 ms |
| everything | 22/25 | 0.767 | 6/6 | 3,449 | 3,008 ms |

\* The hybrid timing excludes query embedding because the earlier "dense" run had
already cached those embeddings; the honest cost of a cold query is the dense row
(~350 ms, almost all of it embedding the question with BGE-large on CPU).

Held-out set (written *after* tuning, never used to choose a threshold or weight):

| Mode | Recall | Off-topic declined |
|---|---|---|
| Old retriever | 6/6 | **0/8** |
| New dense / hybrid | 6/6 | **8/8** |

(MRR on 6 questions: 0.917 old vs 0.833 new. With six questions that difference is one
rank position on one question; it is noise, not a regression I can demonstrate.)

**What this does and does not show.** The big, unambiguous improvement is that the system
now **declines questions that are not in the manual** instead of improvising from
irrelevant text (0 → 14 of 14 across both sets). Plain recall improved by at most one
question (24 → 25), which is within noise. I would not claim better recall from this data.

### 5.2 Decisions the numbers forced (I expected the opposite for two of them)

* **Cross-encoder reranker: off.** It lowered recall (22/25) and added 2-3 s of CPU time.
  The score also could not be used as an abstention signal: correct answers scored as low
  as 0.0025 while off-topic questions reached 0.0066.
* **ColPali (ColSmol-256M): off.** Alone it found the right page for only 4 of 25
  questions; fused, it slightly lowered MRR and added ~1 s. This is a 256 M-parameter
  model running on CPU against a mostly-text 75-page PDF (4 tables, 2 images); the
  expected benefit of ColPali is on documents where layout carries the meaning, and a
  full-size model needs a GPU. The whole path works end-to-end (indexing, sidecar,
  fusion, fallback, tests); enable with `VISUAL_ENABLED=true scripts/run_all.sh`. The LLM
  (Groq) is text-only, so even when enabled, visual retrieval only changes *which
  sections* are retrieved and cited; it does not feed page images to the model.
* **Keyword stage: kept, but restricted.** Naive OR-matching dropped recall from 25 to
  23 because ubiquitous words like "leave" pulled in half the document. Restricting it to
  distinctive terms restored 25/25. Even then it never beat dense-only on MRR on this
  question set (0.933 vs 0.960), so it is weighted at 0.25 as insurance for exact tokens.
  I did not write exact-token questions to demonstrate that benefit; treat it as unproven.

### 5.3 Bugs the evaluation caught in my own code

* The reranker wrapper applied a sigmoid on top of one `CrossEncoder` already applies,
  squashing every score into [0.50, 0.73] (so the first threshold I chose was
  meaningless). Fixed and covered by the re-measurement above.
* Retrieval used to fail completely if only the reranker crashed. Found by a test; each
  stage now degrades independently.
* The streaming parser claimed to handle a stray `</think>` but did not. Found by a test.
* The first lexical implementation hurt recall (above).

### 5.4 Calibration (and its limits)

`ABSTAIN_DENSE_THRESHOLD = 0.575`: on the tuning set the lowest answerable question scored
0.603 and the highest off-topic one 0.548. The margin is **thin** (0.055). The held-out set
(8 new off-topic questions) agreed, but 14 questions is little evidence. If users report
"I couldn't find that" for questions that are in the manual, lower the threshold toward
0.55 and re-run `python -m eval.diagnose`. Re-calibrate whenever the document set or
the embedding model changes.

---

## 6. Not done / known limits

* **OKF meaning** is my assumption (top of this document).
* **Stored breadcrumbs still contain the stray "27"-style headings.** I only cleaned them
  at display time and in the ingester. The stored embeddings were produced from the old
  breadcrumbs; a clean `ingest` from the PDF (about 8 CPU-minutes, free of LLM cost) would
  regenerate them. I did not do it because it changes retrieval and would invalidate the
  numbers above.
* The old LangChain collection (`meritech_policy`, 587 rows) and the old retriever
  (`ingestion/retriever.py`, `app.py`, `ask_bot.py`) are left in place unmodified: the
  evaluation harness uses them as the baseline. Delete them when you no longer need the
  comparison. (Earlier in this project I wrongly said this vector store was empty; it
  holds those 587 rows.)
* The rate limiter is in memory (per process). With several workers the effective limit
  is per worker; use Redis for a shared limit. Login throttling is per client IP, which
  is the proxy's address if deployed behind one without forwarded-IP handling.
* Tokens are stored in `localStorage`. Output is now sanitised, but HTTP-only cookies
  would be stronger. No Content-Security-Policy (the pages use inline scripts). No
  token revocation or password reset.
* The keyword stage only understands English tokens; other languages rely on the dense
  stage (multilingual quality of `bge-large-en` is limited).
* A client closing the browser mid-answer does not cancel the upstream LLM call.
* If the model emits only `</think>` without an opening tag (some Qwen templates), the
  text before it is shown as answer text. It does not do this today (live test).
* LMS parts not exercised: Playwright browser tests (2 skipped), AWS/Terraform deploy,
  Stripe, SMTP e-mail with real credentials.
* ColPali at full size (ColQwen/ColPali 3B) was not run; it needs a GPU. The scaling
  path for many documents is a multivector store (e.g. Qdrant) instead of the in-memory
  `.npy` files.

---

## 7. Where things live

| Area | Files |
|---|---|
| Config (every tunable, env-overridable) | `core/config.py` |
| Knowledge format | `rag/okf.py`, `data/okf/*.okf.jsonl` |
| Storage / search | `rag/store.py` |
| Orchestration | `rag/engine.py`, `rag/fusion.py`, `rag/guardrails.py`, `rag/streaming.py` |
| Optional stages | `rag/reranker.py`, `rag/visual.py`, `visual_service/` |
| Ingestion | `ingestion/pipeline.py` (`ingest`, `migrate-legacy`) |
| API | `main.py`, `auth.py`, `schemas.py` |
| UI | `frontend/index.html`, `frontend/vendor/` |
| Evaluation | `eval/` |
| Tests | `tests/`, `lms_backend/tests/integration/test_sso_provisioning.py` |
| Boot | `scripts/run_all.sh` |
