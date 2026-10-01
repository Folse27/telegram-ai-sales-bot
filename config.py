"""Env settings, tunables and file paths. Tests patch values here."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CATALOG_PATH = DATA_DIR / "catalog.json"
PHOTOS_PATH = DATA_DIR / "photos.json"
OVERRIDES_PATH = DATA_DIR / "overrides.yaml"
CONTENT_PATH = DATA_DIR / "content.yaml"
PROMPT_PATH = DATA_DIR / "prompt.md"
REPLY_TOOL_PATH = DATA_DIR / "reply_tool.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    webhook_secret: str
    public_url: str = ""
    render_external_url: str = ""  # set by Render automatically
    port: int = 8080  # Render sets PORT
    admin_chat_id: int | None = None
    photo_channel_id: int | None = None
    allowed_user_ids: Annotated[list[int], NoDecode] = []  # empty = everyone
    tester_ids: Annotated[list[int], NoDecode] = []  # may use /clear (wipes their own chat's memory)

    # Anthropic
    anthropic_api_key: str = ""
    model: str = "claude-haiku-4-5"  # MODEL env; main.py § LLM adapts request params per model family
    llm_max_tokens: int = 1200  # models that think get at least 4000 (thinking counts toward max_tokens)
    llm_timeout: float = 30.0
    llm_temperature: float = 0.2  # low: Haiku follows the examples more closely; sent only to models that accept it
    llm_effort: str = "low"  # Sonnet 5.5 / Opus 5.x / Fable: chat is latency-sensitive (docs: start at low or medium)
    llm_thinking: str = "adaptive"  # Sonnet 5.5: "adaptive" (thinks only when needed at low effort) or "between_tools"
    cache_ttl: str = "1h"  # prompt cache lifetime: "5m" (cheaper writes) or "1h" (fewer writes when chats pause)
    history_turns: int = 20

    # Postgres (Neon)
    database_url: str = ""
    test_database_url: str = ""

    # Behaviour
    persona_name: str = "Микола"
    max_parts: int = 3  # bot messages per reply the model may send (1–5); order templates don't count
    debounce_seconds: float = 10
    debounce_max_wait: float = 25
    part_gap_min: float = 1
    part_gap_max: float = 3

    @field_validator("admin_chat_id", "photo_channel_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v in ("", None) else v

    @field_validator("cache_ttl")
    @classmethod
    def _ttl(cls, v):
        if v not in ("5m", "1h"):
            raise ValueError("CACHE_TTL must be 5m or 1h (Anthropic offers only these two cache lifetimes)")
        return v

    @field_validator("max_parts")
    @classmethod
    def _parts(cls, v):
        if not 1 <= v <= 5:
            raise ValueError("MAX_PARTS must be 1–5")
        return v

    @field_validator("allowed_user_ids", "tester_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @property
    def webhook_base_url(self) -> str:
        return (self.public_url or self.render_external_url).rstrip("/")

    @property
    def webhook_path(self) -> str:
        return f"/tg/{self.webhook_secret}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
