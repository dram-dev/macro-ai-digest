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

    # Summarizer (Phase 2)
    summarizer_backend: str = Field(default="claude_cli_pro", alias="SUMMARIZER_BACKEND")

    # Obsidian (Phase 3)
    obsidian_vault_path: str = Field(default="", alias="OBSIDIAN_VAULT_PATH")
    obsidian_digest_dir: str = Field(default="80 Digest", alias="OBSIDIAN_DIGEST_DIR")


settings = Settings()
