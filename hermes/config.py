"""Muat konfigurasi dari .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    brain_provider: str = os.getenv("HERMES_BRAIN_PROVIDER", "rule").strip().lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # OpenAI-compatible endpoint (LM Studio, vLLM, Ollama, OpenRouter, Groq, dll.)
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    google_model: str = os.getenv("GOOGLE_MODEL", "gemini-1.5-flash")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_notify: bool = _bool("TELEGRAM_NOTIFY", True)
    telegram_allowed: list = None  # type: ignore[assignment]  # diisi di __post_init__

    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "")
    portal_username: str = os.getenv("PORTAL_USERNAME", "")
    portal_password: str = os.getenv("PORTAL_PASSWORD", "")

    google_service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    kepeg_api_key: str = os.getenv("KEPEG_API_KEY", "")
    kepeg_base_url: str = os.getenv(
        "KEPEG_BASE_URL", "https://kepegawaian.it-dpmptsp.my.id/api/v1"
    ).rstrip("/")

    db_path: Path = None  # type: ignore[assignment]  # diisi di __post_init__
    template_dir: Path = None  # type: ignore[assignment]
    output_dir: Path = None  # type: ignore[assignment]
    log_level: str = os.getenv("HERMES_LOG_LEVEL", "INFO").upper()
    web_host: str = os.getenv("HERMES_WEB_HOST", "127.0.0.1")
    web_port: int = int(os.getenv("HERMES_WEB_PORT", "10000"))
    web_enabled: bool = _bool("HERMES_WEB_ENABLED", True)
    # Web login = password saja (tanpa username). Password dibaca dari Docker secret.
    web_pass: str = os.getenv("HERMES_WEB_PASS", "").strip()

    def __post_init__(self) -> None:
        raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
        self.telegram_allowed = [
            int(x) for x in raw.replace(";", ",").split(",") if x.strip()
        ]
        self.db_path = ROOT / os.getenv("HERMES_DB_PATH", "data/hermes.sqlite")
        self.template_dir = ROOT / os.getenv("HERMES_TEMPLATE_DIR", "templates")
        self.output_dir = ROOT / os.getenv("HERMES_OUTPUT_DIR", "output")

        # Kredensial Google Sheet: Docker secret lebih diprioritaskan.
        if not self.google_service_account_file:
            sa_secret = "/run/secrets/google_sa"
            if os.path.isfile(sa_secret):
                self.google_service_account_file = sa_secret

        # Web password: Docker secret file lebih diprioritaskan daripada env.
        secret_file = os.getenv("HERMES_WEB_PASS_FILE", "/run/secrets/web_pass")
        if os.path.isfile(secret_file):
            val = open(secret_file).read().strip()
            if val:
                self.web_pass = val


def get_config() -> Config:
    return Config()
