"""Config loader — reads .env via pydantic-settings, exposes typed Settings."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
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
    summarizer_backend: str = Field(default="mlx_local", alias="SUMMARIZER_BACKEND")
    summarizer_model: str = Field(default="sonnet", alias="SUMMARIZER_MODEL")
    summarizer_max_per_run: int = Field(default=75, alias="SUMMARIZER_MAX_PER_RUN")
    summarizer_max_per_source: int = Field(default=15, alias="SUMMARIZER_MAX_PER_SOURCE")
    # Keep-items older than this never get summarized (0 disables the age-out).
    # Stops capped-out sources (RSS) from accumulating an ever-growing backlog.
    summarizer_max_age_days: int = Field(default=30, alias="SUMMARIZER_MAX_AGE_DAYS")
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
    # Max items kept in Topics/<Topic>.md; older entries roll over into frozen
    # per-month files under Topics/Archive/ (0 keeps everything in one file).
    obsidian_topic_archive_cap: int = Field(default=200, alias="OBSIDIAN_TOPIC_ARCHIVE_CAP")

    # Yahoo Finance watchlist (Tier 2)
    yahoo_tickers: str = Field(
        default="NVDA,AMD,TSM,MSFT,GOOGL,META,AMZN,INTC,AVGO,ASML",
        alias="YAHOO_TICKERS",
    )

    # MLX-LM local server (Apple Silicon)
    mlx_server_url: str = Field(default="http://localhost:8080", alias="MLX_SERVER_URL")
    mlx_model: str = Field(default="mlx-community/Qwen3.6-27B-4bit", alias="MLX_MODEL")

    # Clipped X-post / investigate folder (Phase 3.5)
    # Folder inside the vault where you drop Obsidian Web Clipper .md files.
    # Defaults to "77_Claude_Investigate". If set as an absolute path, it wins;
    # otherwise it's interpreted as relative to OBSIDIAN_VAULT_PATH.
    obsidian_clip_dir: str = Field(
        default="77_Claude_Investigate", alias="OBSIDIAN_CLIP_DIR"
    )

    # Quantitative ingestor thresholds — tune from .env without code changes
    cboe_sigma_thresh: float = Field(default=1.5, alias="CBOE_SIGMA_THRESH")
    cftc_sigma_thresh: float = Field(default=1.2, alias="CFTC_SIGMA_THRESH")
    yahoo_move_thresh_pct: float = Field(default=2.5, alias="YAHOO_MOVE_THRESH_PCT")
    yahoo_rsi_overbought: float = Field(default=75.0, alias="YAHOO_RSI_OVERBOUGHT")
    yahoo_rsi_oversold: float = Field(default=28.0, alias="YAHOO_RSI_OVERSOLD")
    hn_min_points: int = Field(default=100, alias="HN_MIN_POINTS")

    # Full-text extraction — when a feed ships only a teaser (RSS/Substack
    # summary, HN external link), fetch the source article and extract the main
    # body so triage + summarize see real content instead of a snippet. Set
    # FULLTEXT_ENABLED=false to keep raw feed excerpts.
    fulltext_enabled: bool = Field(default=True, alias="FULLTEXT_ENABLED")
    # A feed body shorter than this (chars) is treated as an excerpt worth
    # expanding via a source fetch.
    fulltext_min_chars: int = Field(default=600, alias="FULLTEXT_MIN_CHARS")
    # Cap extracted body length so a long article can't blow the triage /
    # summarize token budget.
    fulltext_max_chars: int = Field(default=8000, alias="FULLTEXT_MAX_CHARS")
    fulltext_timeout_sec: int = Field(default=12, alias="FULLTEXT_TIMEOUT_SEC")

    # Telegram push notifications — terse mobile alerts for high-signal items.
    # No-op (sends nothing) unless both token + chat id are set. Get them from
    # @BotFather (token) and getUpdates / @userinfobot (chat id).
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    notify_enabled: bool = Field(default=True, alias="NOTIFY_ENABLED")
    # Triage score a new item must reach to earn a push. 0.80 = "Balanced".
    notify_min_score: float = Field(default=0.80, alias="NOTIFY_MIN_SCORE")
    # Cap pushes per pipeline run so one busy day can't spam the phone.
    notify_max_per_run: int = Field(default=5, alias="NOTIFY_MAX_PER_RUN")
    # Optional once-per-run "Brief ready" ping. Off by default.
    notify_brief_ping: bool = Field(default=False, alias="NOTIFY_BRIEF_PING")

    # Databricks medallion sink (cross-domain lakehouse). All writes no-op when
    # databricks_enabled=False. Shared-catalog model: one catalog (`digest`),
    # domain-prefixed schemas — macro uses macro_bronze/macro_silver/macro_gold
    # (pc-insurance-digest uses pc_*). See sql/databricks/ for DDL.
    databricks_enabled: bool = Field(default=False, alias="DATABRICKS_ENABLED")
    databricks_host: str = Field(default="", alias="DATABRICKS_HOST")
    databricks_http_path: str = Field(default="", alias="DATABRICKS_HTTP_PATH")
    databricks_token: str = Field(default="", alias="DATABRICKS_TOKEN")
    databricks_catalog: str = Field(default="digest", alias="DATABRICKS_CATALOG")
    databricks_schema_prefix: str = Field(default="macro_", alias="DATABRICKS_SCHEMA_PREFIX")

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("summarizer_model", mode="before")
    @classmethod
    def _validate_model_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-._]*", str(v)):
            raise ValueError(
                f"SUMMARIZER_MODEL must contain only letters, digits, hyphens, dots, "
                f"or underscores — got: {v!r}"
            )
        return v

    @field_validator("ollama_host", "mlx_server_url", mode="before")
    @classmethod
    def _validate_localhost_url(cls, v: str) -> str:
        hostname = urlparse(str(v)).hostname
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                f"URL must point to localhost for safety, got hostname: {hostname!r}"
            )
        return v

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _strip_bot_prefix(cls, v: str) -> str:
        """Tolerate a token pasted with the URL's 'bot' prefix (.../bot<TOKEN>).

        Real tokens always start with the bot's numeric id, so a leading 'bot'
        is the doubled-prefix mistake that yields a 404 from the Telegram API.
        """
        v = str(v).strip()
        if re.match(r"(?i)^bot\d", v):
            v = v[3:]
        return v


settings = Settings()
