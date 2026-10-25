"""Shared test fixtures: a virtual clock, a recording Telegram transport, a scripted pipeline, the sample shop,
and a throwaway Postgres schema per test (TEST_DATABASE_URL; tests that need it are skipped without it)."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from salesbot.catalog import Catalog
from salesbot.config import BASE_DIR, SAMPLE_SHOP_DIR, Settings
from salesbot.llm import build_system
from salesbot.orders import Orders
from salesbot.sessions import Incoming, Reply
from salesbot.shop import ShopData
from salesbot.storage import Storage
from salesbot.templates import Templates
from salesbot.transport import OutPhoto
from salesbot.validation import Validator, reference_numbers

ADMIN = -100500

# ── the fictional sample shop, shared by the pure tests ──
SHOP = ShopData.from_folder(SAMPLE_SHOP_DIR)
CAT = Catalog(SHOP.catalog, SHOP.synonyms)
CONTENT = SHOP.content
TPL = Templates(CONTENT, CAT, "Микола")
VAL = Validator(CAT, CONTENT)
ORD = Orders(CAT, TPL)
PERSONA = SimpleNamespace(persona_name="Микола")
ALLOWED = reference_numbers(build_system(PERSONA, CAT, CONTENT, SHOP.examples)) | {"1"}


def settings(**overrides: Any) -> Settings:
    """Complete settings without reading .env or the environment."""
    base: dict[str, Any] = {"telegram_bot_token": "1:x", "webhook_secret": "x", "anthropic_api_key": "test",
                            "database_url": "postgresql://localhost/test", "admin_chat_id": ADMIN}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


class TestEnv(BaseSettings):
    """Only what the tests need from .env or the environment (CI sets TEST_DATABASE_URL to a Postgres service)."""

    __test__ = False  # not a test class
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    test_database_url: str = ""


@pytest.fixture
async def storage():
    dsn = TestEnv().test_database_url
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = f"t_{uuid.uuid4().hex[:10]}"
    st = await Storage.connect(dsn, schema=schema)
    yield st
    await st.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await st.close()


async def settle(rounds: int = 50) -> None:
    """Let every ready task run until the loop is quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Virtual time: sleep() parks until advance() moves the clock past the wake time."""

    def __init__(self) -> None:
        self.t = 0.0
        self._sleepers: list = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.t + seconds, next(self._seq), fut))
        await fut

    async def advance(self, seconds: float) -> None:
        target = self.t + seconds
        await settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            when, _, fut = heapq.heappop(self._sleepers)
            if fut.done():  # its task was cancelled
                continue
            self.t = max(self.t, when)
            fut.set_result(None)
            await settle()
        self.t = target
        await settle()


class FakeTransport:
    """Records everything the bot would send to Telegram."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._ids = itertools.count(1000)
        self.reject_file_ids = False  # simulate Telegram refusing stale file_ids once

    def of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    async def send_text(self, chat_id, text, *, reply_markup=None, reply_to=None) -> int:
        self.calls.append(("text", chat_id, text, reply_markup, reply_to))
        return next(self._ids)

    async def send_photos(self, chat_id, photos: list[OutPhoto], caption=None) -> list[str | None]:
        if self.reject_file_ids and any(p.file_id for p in photos):
            self.reject_file_ids = False
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import SendPhoto
            raise TelegramBadRequest(SendPhoto(chat_id=chat_id, photo="x"), "Bad Request: wrong file identifier")
        sent = [p.file_id or f"upload:{p.src}" for p in photos]
        self.calls.append(("photos", chat_id, sent, caption))
        return [p.file_id or f"fid:{p.src}" for p in photos]

    async def send_typing(self, chat_id) -> None:
        self.calls.append(("typing", chat_id))

    async def copy_message(self, to_chat_id, from_chat_id, message_id, reply_to=None) -> int:
        self.calls.append(("copy", to_chat_id, from_chat_id, message_id, reply_to))
        return next(self._ids)

    async def edit_markup(self, chat_id, message_id, markup) -> None:
        self.calls.append(("edit_markup", chat_id, message_id, markup))


class FakePipeline:
    """Scripted stand-in for the LLM pipeline: generation takes `gen_time` virtual seconds."""

    def __init__(self, clock: FakeClock, gen_time: float = 2.0, parts: int = 1, human: bool = False):
        self.clock, self.gen_time, self.n_parts, self.human = clock, gen_time, parts, human
        self.calls: list[list[str]] = []  # batch texts, one entry per generate() call (incl. cancelled)
        self.call_times: list[float] = []
        self.sent: list[tuple[float, str]] = []
        self.committed: list[tuple[list[str], list[str]]] = []
        self.relayed: list[str] = []
        self.extras: list[dict] = []  # reply.extra per commit (e.g. first_part_after_s)
        self.errors: list[list[str]] = []
        self.fail_next = False

    async def is_human(self, chat_id: int) -> bool:
        return self.human

    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None:
        self.relayed += [m.text for m in batch]

    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply:
        texts = [m.text for m in batch]
        self.calls.append(texts)
        self.call_times.append(self.clock.now())
        await self.clock.sleep(self.gen_time)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("LLM down")
        return Reply([f"{'+'.join(texts)}#{i}" for i in range(self.n_parts)])

    async def send_part(self, chat_id: int, part) -> None:
        self.sent.append((self.clock.now(), part))

    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list, reply: Reply) -> None:
        self.committed.append(([m.text for m in batch], list(sent_parts)))
        self.extras.append(dict(reply.extra))

    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None:
        self.errors.append([m.text for m in batch])
