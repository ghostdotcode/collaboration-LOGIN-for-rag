import sys
import io
import json
import time
import re
import threading
from pathlib import Path
from flask import Flask, request, Response, send_from_directory
from flask_cors import CORS

# Force UTF-8 output (Useful for Windows, harmless on Linux)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.append(str(Path(__file__).resolve().parent))



app = Flask(__name__, static_folder="ui")
CORS(app)

# ── Retriever (lazy, initialized once in background) ─────────────────────────
_retriever = None
_retriever_ready = threading.Event()
_retriever_error = None


def _init_retriever():
    global _retriever, _retriever_error
    try:
        from ingestion.retriever import RAGRetriever
        _retriever = RAGRetriever()
        print("[App] Retriever ready.")
    except Exception as e:
        _retriever_error = str(e)
        print(f"[App] Retriever init failed: {e}")
    finally:
        _retriever_ready.set()


threading.Thread(target=_init_retriever, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("ui", "index.html")


@app.route("/api/status")
def status():
    ready = _retriever_ready.is_set() and _retriever is not None
    return {"ready": ready, "error": _retriever_error}


@app.route("/api/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()

    if not query:
        return {"error": "No query provided"}, 400

    def generate():
        # ── Wait for retriever ─────────────────────────────────────────────
        if not _retriever_ready.is_set():
            yield _sse({"type": "status", "content": "Initializing knowledge base, please wait…"})
            _retriever_ready.wait(timeout=60)

        if _retriever is None:
            yield _sse({"type": "error", "content": _retriever_error or "Retriever failed to initialize."})
            return

        # ── Signal that retrieval is starting ──────────────────────────────
        yield _sse({"type": "status", "content": "Searching knowledge base…"})

        try:
            full_response = _retriever.answer_question(query)
        except Exception as e:
            yield _sse({"type": "error", "content": str(e)})
            return

        # ── Parse <think>…</think> from Qwen's response ───────────────────
        think_match = re.search(r"<think>(.*?)</think>", full_response, re.DOTALL)
        if think_match:
            thinking_raw = think_match.group(1).strip()
            answer_raw   = full_response[think_match.end():].strip()
        else:
            thinking_raw = ""
            answer_raw   = full_response.strip()

        # ── Stream thinking safely (preserves formatting & newlines) ───────
        if thinking_raw:
            # Using regex split to keep all whitespace characters, including \n
            think_tokens = re.split(r'(\s+)', thinking_raw)
            for token in think_tokens:
                if token:
                    yield _sse({"type": "thinking", "content": token})
                    time.sleep(0.005)  # Faster stream for thinking block

        yield _sse({"type": "thinking_done"})

        # ── Stream answer safely (preserves formatting & newlines) ─────────
        answer_tokens = re.split(r'(\s+)', answer_raw)
        for token in answer_tokens:
            if token:
                yield _sse({"type": "answer", "content": token})
                time.sleep(0.015)  # Slightly slower for readability

        yield _sse({"type": "done"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n[Meritech AI]  ->  http://localhost:5000\n")
    app.run(debug=False, port=5000, threaded=True)