import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from .env file into environment (real env vars win).
load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


class Config:
    """
    Centralised configuration loader.

    Everything tunable lives here and is overridable by environment variable,
    so retrieval behaviour can be changed (and A/B-tested by the eval harness)
    without editing code.
    """
    # --- Project Directories ---
    SRC_DIR: Path = Path(__file__).resolve().parent
    BASE_DIR: Path = SRC_DIR.parent
    DATA_DIR: Path = BASE_DIR / "data"
    RAW_DIR: Path = DATA_DIR / "raw"
    PROCESSED_DIR: Path = DATA_DIR / "processed"
    OKF_DIR: Path = DATA_DIR / "okf"
    VISUAL_DIR: Path = DATA_DIR / "visual"

    for _d in (RAW_DIR, PROCESSED_DIR, OKF_DIR, VISUAL_DIR):
        _d.mkdir(parents=True, exist_ok=True)

    # --- Environment ---
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "local")

    # --- API Keys & External Services ---
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_CHANNEL_ID: str = os.getenv("SLACK_CHANNEL_ID", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_BASE_URL: str = os.getenv("JIRA_BASE_URL", "")
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")

    # --- Database (pgvector) ---
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # --- Web / security ---
    CORS_ORIGINS: list[str] = _csv(
        "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    )
    ASK_RATE_LIMIT_PER_MIN: int = _int("ASK_RATE_LIMIT_PER_MIN", 20)
    MAX_QUERY_CHARS: int = _int("MAX_QUERY_CHARS", 1000)
    MAX_HISTORY_TURNS: int = _int("MAX_HISTORY_TURNS", 3)

    # --- Models ---
    EMBED_MODEL: str = os.getenv("EMBED_MODEL", "BAAI/bge-large-en-v1.5")
    EMBED_DIM: int = _int("EMBED_DIM", 1024)
    RERANK_MODEL: str = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")
    # Off by default: measured on the golden set it added 2-3 s of CPU latency and did not
    # improve recall over dense+distinctive-lexical (docs/RAG_UPGRADE.md, "Measured results").
    RERANK_ENABLED: bool = _bool("RERANK_ENABLED", False)
    GENERATION_MODEL: str = os.getenv("GENERATION_MODEL", "qwen/qwen3.8-27b")
    GENERATION_MAX_TOKENS: int = _int("GENERATION_MAX_TOKENS", 1024)
    GENERATION_TIMEOUT_S: float = _float("GENERATION_TIMEOUT_S", 45.0)

    # --- Retrieval ---
    DENSE_TOP_N: int = _int("DENSE_TOP_N", 30)
    LEXICAL_TOP_N: int = _int("LEXICAL_TOP_N", 30)
    # Only query terms found in <= this share of passages take part in lexical search (1.0 = all).
    LEXICAL_MAX_DF: float = _float("LEXICAL_MAX_DF", 0.08)
    RERANK_TOP_N: int = _int("RERANK_TOP_N", 8)
    RERANK_MAX_LENGTH: int = _int("RERANK_MAX_LENGTH", 320)
    RERANK_PASSAGES_PER_UNIT: int = _int("RERANK_PASSAGES_PER_UNIT", 1)
    # Lexical hits are noisy for natural-language questions (every question shares
    # words like "leave"/"days"), so they get a fraction of the dense vote in RRF.
    LEXICAL_WEIGHT: float = _float("LEXICAL_WEIGHT", 0.25)
    FINAL_TOP_K: int = _int("FINAL_TOP_K", 3)
    CONTEXT_CHAR_BUDGET: int = _int("CONTEXT_CHAR_BUDGET", 9000)
    RRF_K: int = _int("RRF_K", 60)
    # Abstain (answer "not in the manual") when the best dense cosine similarity is
    # below this. Calibrated on eval/golden.jsonl: answerable min 0.603, out-of-scope
    # max 0.548 -> midpoint 0.575. The margin is thin and the sample small; re-run
    # `python -m eval.diagnose` after changing documents or the embedding model.
    ABSTAIN_DENSE_THRESHOLD: float = _float("ABSTAIN_DENSE_THRESHOLD", 0.575)
    # Optional second gate on the reranker probability. 0 = disabled, because on this
    # corpus correct answers scored as low as 0.0025 vs 0.0066 for off-topic ones.
    ABSTAIN_RERANK_THRESHOLD: float = _float("ABSTAIN_RERANK_THRESHOLD", 0.0)
    RETRIEVAL_CACHE_TTL_S: int = _int("RETRIEVAL_CACHE_TTL_S", 300)

    # --- Visual retrieval (ColPali family) ---
    # Off by default: measured, the CPU-sized ColSmol-256M found the answer page for only
    # 4/25 questions alone and slightly lowered MRR when fused (docs/RAG_UPGRADE.md).
    # Fully working and tested; switch on with VISUAL_ENABLED=true (+ run the sidecar).
    VISUAL_ENABLED: bool = _bool("VISUAL_ENABLED", False)
    VISUAL_SERVICE_URL: str = os.getenv("VISUAL_SERVICE_URL", "http://127.0.0.1:8002")
    VISUAL_TOP_N: int = _int("VISUAL_TOP_N", 5)
    VISUAL_TIMEOUT_S: float = _float("VISUAL_TIMEOUT_S", 8.0)
    VISUAL_MODEL: str = os.getenv("VISUAL_MODEL", "vidore/colSmol-256M")

    @classmethod
    def validate(cls) -> None:
        """Raise an error if any required key is missing."""
        required = ["GROQ_API_KEY", "DATABASE_URL"]
        missing = [key for key in required if not getattr(cls, key)]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
