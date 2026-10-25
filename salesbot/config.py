"""Environment settings and file paths. A missing required variable stops the process with a clear message."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROMPT_PATH = DATA_DIR / "prompt.md"
REPLY_TOOL_PATH = DATA_DIR / "reply_tool.json"
SAMPLE_SHOP_DIR = DATA_DIR / "sample"

REQUIRED = ("telegram_bot_token", "webhook_secret", "anthropic_api_key", "database_url", "admin_chat_id")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    webhook_secret: str
    admin_chat_id: int  # staff group for handoff cards and admin commands (the bot must be an admin there)
    public_url: str = ""
    render_external_url: str = ""  # set by Render automatically
    port: int = 8080  # Render sets PORT
    allowed_user_ids: Annotated[list[int], NoDecode] = []  # empty = everyone
    tester_ids: Annotated[list[int], NoDecode] = []  # may use /clear; not counted against the per-user limit

    # Anthropic
    anthropic_api_key: str
    model: str = "claude-haiku-4-5"  # salesbot/llm.py adapts request params per model family
    llm_max_tokens: int = 1200  # models that think get at least 4000 (thinking counts toward max_tokens)
    llm_timeout: float = 30.0
    llm_temperature: float = 0.2  # sent only to models that accept sampling params
    llm_effort: str = "low"  # newer models: chat is latency-sensitive
    llm_thinking: str = "adaptive"  # Sonnet 5.5: "adaptive" or "between_tools"
    cache_ttl: str = "1h"  # prompt cache lifetime: "5m" (cheaper writes) or "1h" (fewer writes when chats pause)
    history_turns: int = 20

    # Database and shop data
    database_url: str
    shop_source: str = "data/sample"  # a shop folder (relative to the repo) or "db" (uploaded with salesbot.tools)

    # Behaviour
    persona_name: str = "Микола"
    max_parts: int = 3  # bot messages per reply the model may send (1–5); order templates don't count
    debounce_seconds: float = 10
    debounce_max_wait: float = 25
    part_gap_min: float = 1
    part_gap_max: float = 3

    log_format: Literal["text", "json"] = "text"  # json: one object per line with structured fields (production)

    # Usage limits (0 = no limit)
    daily_replies_per_user: int = 50  # AI replies per client in 24 h
    daily_budget_usd: float = 3.0  # estimated AI spend of the whole bot in 24 h

    @field_validator("cache_ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError("CACHE_TTL must be 5m or 1h (Anthropic offers only these two cache lifetimes)")
        return v

    @field_validator("max_parts")
    @classmethod
    def _parts(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("MAX_PARTS must be 1–5")
        return v

    @field_validator("allowed_user_ids", "tester_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator(*REQUIRED, mode="before")
    @classmethod
    def _not_blank(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            raise ValueError("is empty")
        return v

    @property
    def webhook_base_url(self) -> str:
        return (self.public_url or self.render_external_url).rstrip("/")

    @property
    def webhook_path(self) -> str:
        return f"/tg/{self.webhook_secret}"

    @property
    def shop_dir(self) -> Path | None:
        """The shop folder, or None when the shop data lives in the database."""
        return None if self.shop_source == "db" else (BASE_DIR / self.shop_source).resolve()


class ConfigError(SystemExit):
    """Raised instead of a pydantic traceback: names every bad variable and where to look."""


def describe(error: ValidationError) -> str:
    lines = []
    for e in error.errors():
        name = str(e["loc"][0]).upper() if e["loc"] else "?"
        lines.append(f"  {name}: {'missing' if e['type'] == 'missing' else e['msg']}")
    return "Configuration error (see .env.example):\n" + "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # pydantic-settings fills the required fields from the environment
    except ValidationError as e:
        raise ConfigError(describe(e)) from None
