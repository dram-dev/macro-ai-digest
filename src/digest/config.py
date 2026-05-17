"""Config loader — reads .env via pydantic-settings, exposes typed Settings."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    db_path: Path = Field(default=Path("./data/state.db"), alias="DB_PATH")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Reddit
    reddit_client_id: str = Field(default="", alias="REDDIT_CLIENT_ID")
    reddit_client_secret: str = Field(default="", alias="REDDIT_CLIENT_SECRET")
    reddit_user_agent: str = Field(default="macro-ai-digest/0.1", alias="REDDIT_USER_AGENT")

    # FRED
    fred_api_key: str = Field(default="", alias="FRED_API_KEY")

    # EDGAR
    edgar_user_agent: str = Field(default="", alias="EDGAR_USER_AGENT")

    # Gmail
    gmail_credentials_path: Path = Field(
        default=Path("./secrets/gmail_credentials.json"),
        alias="GMAIL_CREDENTIALS_PATH",
    )
    gmail_token_path: Path = Field(
        default=Path("./secrets/gmail_token.json"),
        alias="GMAIL_TOKEN_PATH",
    )
    gmail_label: str = Field(default="Digest/Economist", alias="GMAIL_LABEL")
    gmail_lookback_days: int = Field(default=14, alias="GMAIL_LOOKBACK_DAYS")

    # Summarizer (Phase 2)
    summarizer_backend: str = Field(default="claude_cli_pro", alias="SUMMARIZER_BACKEND")
    summarizer_model: str = Field(default="sonnet", alias="SUMMARIZER_MODEL")
    summarizer_max_per_run: int = Field(default=20, alias="SUMMARIZER_MAX_PER_RUN")
    summarizer_timeout_sec: int = Field(default=120, alias="SUMMARIZER_TIMEOUT_SEC")

    # Optional API keys for fallback summarizer backends
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # Triage (Phase 2)
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5:14b", alias="OLLAMA_MODEL")
    triage_min_score: float = Field(default=0.5, alias="TRIAGE_MIN_SCORE")
    triage_lookback_hours: int = Field(default=24, alias="TRIAGE_LOOKBACK_HOURS")

    # Obsidian (Phase 3)
    obsidian_vault_path: str = Field(default="", alias="OBSIDIAN_VAULT_PATH")
    obsidian_digest_dir: str = Field(default="80 Digest", alias="OBSIDIAN_DIGEST_DIR")

    # Yahoo Finance watchlist (Tier 2)
    yahoo_tickers: str = Field(
        default="NVDA,AMD,TSM,MSFT,GOOGL,META,AMZN,INTC,AVGO,ASML",
        alias="YAHOO_TICKERS",
    )

    # Clipped X-post / investigate folder (Phase 3.5)
    # Folder inside the vault where you drop Obsidian Web Clipper .md files.
    # Defaults to "77_Claude_Investigate". If set as an absolute path, it wins;
    # otherwise it's interpreted as relative to OBSIDIAN_VAULT_PATH.
    obsidian_clip_dir: str = Field(
        default="77_Claude_Investigate", alias="OBSIDIAN_CLIP_DIR"
    )


settings = Settings()
