import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from .env file into environment
load_dotenv()

class Config:
    """
    Centralised configuration loader.
    """
    # --- Project Directories ---
    SRC_DIR: Path = Path(__file__).resolve().parent
    BASE_DIR: Path = SRC_DIR.parent
    DATA_DIR: Path = BASE_DIR / "data"
    RAW_DIR: Path = DATA_DIR / "raw"
    PROCESSED_DIR: Path = DATA_DIR / "processed"

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # --- API Keys & External Services ---
    # Groq (free, fast LLM provider)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # Slack
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_CHANNEL_ID: str = os.getenv("SLACK_CHANNEL_ID", "")

    # Jira
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_BASE_URL: str = os.getenv("JIRA_BASE_URL", "")
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")

    # Database (pgvector)
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    @classmethod
    def validate(cls) -> None:
        """Raise an error if any required key is missing."""
        required = ["GROQ_API_KEY", "DATABASE_URL"]
        missing = [key for key in required if not getattr(cls, key)]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )