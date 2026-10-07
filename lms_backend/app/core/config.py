"""
Centralized application configuration for the LMS backend.

All values are sourced from environment variables (12-factor). Nothing in
here reaches out to the network or the database at import time so that the
module stays safe to import from Alembic, Celery workers and tests alike.
"""

from functools import lru_cache
from typing import Annotated, List, Literal, Optional

from pydantic import computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── General ──────────────────────────────────────────────────────────
    PROJECT_NAME: str = "Leave Management System"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: Literal["local", "test", "staging", "production"] = "local"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # ── Multi-tenancy ────────────────────────────────────────────────────
    TENANT_HEADER: str = "X-Tenant-ID"
    # Postgres schema the LMS owns. Kept separate from the RAG chatbot's
    # public schema so both services can share one physical database.
    DB_SCHEMA: str = "lms"

    # ── Security / SSO (shared with the existing RAG Chatbot) ───────────
    # NOTE: the RAG bot signs its JWTs with JWT_SECRET_KEY; reusing the same
    # secret + algorithm is what makes zero-friction SSO possible.
    SECRET_KEY: str = "fallback-super-secret-key-for-local-dev-only"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Just-in-time provisioning: when a chatbot-authenticated person has no LMS
    # profile yet, create an EMPLOYEE record instead of dead-ending them with
    # "ask HR to add you". Off by default (opt-in per deployment); when on, the
    # tenant's seat limit still applies and every creation is audited.
    SSO_AUTO_PROVISION: bool = False
    # Optional allow-list of email domains eligible for auto-provisioning
    # (empty = any domain). Use it so a public sign-up form cannot mint staff.
    SSO_ALLOWED_EMAIL_DOMAINS: Annotated[List[str], NoDecode] = []
    # One-click approve/reject links embedded in emails.
    ACTION_TOKEN_EXPIRE_HOURS: int = 72
    REFRESH_COOKIE_NAME: str = "lms_refresh"
    COOKIE_DOMAIN: Optional[str] = None
    COOKIE_SECURE: bool = True

    # ── Database (PostgreSQL 16, async SQLAlchemy 2.0 + asyncpg) ────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/meritech_db"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_ECHO: bool = False
    # Seconds a transaction may hold a row lock before Postgres aborts it.
    # Guards the SELECT ... FOR UPDATE path against a wedged connection.
    DB_LOCK_TIMEOUT_MS: int = 5_000

    # ── Cache / session sharing with RAG bot (Redis) ────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Rate limiting (Redis sliding window) ────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_IP: int = 120  # requests per window
    RATE_LIMIT_PER_TENANT: int = 3_000
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # ── Message broker / background jobs (Celery + RabbitMQ) ────────────
    CELERY_BROKER_URL: str = "amqp://guest:guest@localhost:5672//"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    # Midnight accrual fan-out: users processed per Celery task.
    ACCRUAL_CHUNK_SIZE: int = 1_000

    # ── Object storage for leave attachments (S3 pre-signed URLs) ───────
    S3_BUCKET: Optional[str] = None
    S3_REGION: str = "us-east-1"
    S3_PRESIGN_EXPIRY_SECONDS: int = 900
    MAX_ATTACHMENT_MB: int = 10

    # ── Email (Celery email_worker) ─────────────────────────────────────
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    EMAIL_FROM: str = "no-reply@meritech.example"
    # Base URL used to build one-click approval links in emails.
    APP_BASE_URL: str = "http://localhost:8001"

    # ── SaaS billing ────────────────────────────────────────────────────
    STRIPE_API_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    # ── Leave defaults (tenant-overridable) ─────────────────────────────
    # Mon-Fri; 0 = Monday per date.weekday().
    DEFAULT_WORKING_DAYS: Annotated[List[int], NoDecode] = [0, 1, 2, 3, 4]
    DEFAULT_TIMEZONE: str = "UTC"
    # Warn (don't block) when this many teammates are already away.
    CONFLICT_WARNING_THRESHOLD: int = 2

    # ── CORS (allow the RAG chatbot origin to call this API) ────────────
    # NoDecode keeps pydantic-settings from JSON-parsing the raw env value, so
    # the CSV form below reaches `_split_csv` instead of erroring first.
    BACKEND_CORS_ORIGINS: Annotated[List[str], NoDecode] = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    @field_validator(
        "BACKEND_CORS_ORIGINS", "DEFAULT_WORKING_DAYS", "SSO_ALLOWED_EMAIL_DOMAINS", mode="before"
    )
    @classmethod
    def _split_csv(cls, v):
        """Accept both JSON (`["a","b"]`) and CSV (`a,b`) env var styles."""
        if isinstance(v, str) and not v.strip().startswith("["):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("DEFAULT_WORKING_DAYS")
    @classmethod
    def _validate_working_days(cls, v: List[int]) -> List[int]:
        days = [int(d) for d in v]
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("DEFAULT_WORKING_DAYS must be weekday ints in 0..6")
        return sorted(set(days))

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> "Settings":
        if self.ENVIRONMENT == "production":
            if "local-dev-only" in self.SECRET_KEY or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "SECRET_KEY must be a strong, non-default value in production"
                )
            if self.DEBUG:
                raise ValueError("DEBUG must be False in production")
        return self

    # ── Derived values ──────────────────────────────────────────────────
    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """DATABASE_URL normalised to the asyncpg driver."""
        url = self.DATABASE_URL
        if url.startswith("postgresql+psycopg2://"):
            return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Sync driver URL — Alembic migrations and Celery workers use this."""
        url = self.DATABASE_URL
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so tests can override via `get_settings.cache_clear()`."""
    return Settings()


settings = get_settings()
