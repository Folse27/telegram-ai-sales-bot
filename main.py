"""Обрій Тех demo sales-manager bot: everything that runs on the server (PLAN.md §3).

Run on Render:        python main.py              (webhook, aiohttp)
Run locally (dev):    python main.py --polling    (deletes the webhook, long-polls)
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto, LinkPreviewOptions, Message, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from config import Settings, get_settings

log = logging.getLogger("Обрій")


# ═══════════════════════════ 1. Storage ═══════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    chat_id          bigint PRIMARY KEY,
    user_id          bigint,
    username         text,
    full_name        text,
    mode             text NOT NULL DEFAULT 'BOT' CHECK (mode IN ('BOT', 'HUMAN')),
    mode_changed_at  timestamptz NOT NULL DEFAULT now(),
    last_manager_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seen_updates (
    update_id  bigint PRIMARY KEY,
    seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id             bigserial PRIMARY KEY,
    chat_id        bigint NOT NULL REFERENCES clients (chat_id),
    role           text NOT NULL CHECK (role IN ('user', 'assistant', 'manager')),
    text           text NOT NULL,
    tg_message_id  bigint,
    answered       boolean,  -- user rows only: false until a reply batch covering it was delivered
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_chat_idx ON messages (chat_id, id);
CREATE INDEX IF NOT EXISTS messages_unanswered_idx ON messages (created_at) WHERE role = 'user' AND NOT answered;
CREATE TABLE IF NOT EXISTS orders (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    status           text NOT NULL,
    data             jsonb NOT NULL DEFAULT '{}',
    payment_sent_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_editable ON orders (chat_id)
    WHERE status IN ('DRAFT', 'AWAITING_CONFIRMATION');
CREATE TABLE IF NOT EXISTS handoffs (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    reason           text NOT NULL,
    department       text,
    summary          text,
    card_message_id  bigint,
    status           text NOT NULL DEFAULT 'open',
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz
);
CREATE TABLE IF NOT EXISTS relay_map (
    admin_message_id  bigint PRIMARY KEY,
    chat_id           bigint NOT NULL,
    handoff_id        bigint REFERENCES handoffs (id),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  bigserial PRIMARY KEY,
    chat_id             bigint,
    model               text,
    attempt             int,
    input_tokens        int,
    output_tokens       int,
    cache_read_tokens   int,
    cache_write_tokens  int,
    latency_ms          int,
    status              text,
    validation          jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
"""


async def create_pool(dsn: str, schema: str | None = None) -> asyncpg.Pool:
    async def init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def use_schema(conn: asyncpg.Connection) -> None:
        # Neon's proxy drops search_path as a startup parameter, and the pool runs RESET ALL on release,
        # so the test schema is set on every acquire.
        await conn.execute(f'SET search_path TO "{schema}"')

    # No idle connections are kept (min_size=0, closed after 60 s idle) so Neon can scale to zero.
    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=5, max_inactive_connection_lifetime=60, command_timeout=15, init=init,
        setup=use_schema if schema else None,
    )


class Storage:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._seen: OrderedDict[int, None] = OrderedDict()  # in-memory LRU in front of seen_updates

    @classmethod
    async def connect(cls, dsn: str, schema: str | None = None) -> Storage:
        """Open the pool and create tables if missing. `schema` isolates tests (one throwaway schema per case)."""
        if schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            finally:
                await conn.close()
        st = cls(await create_pool(dsn, schema))
        await st.pool.execute(SCHEMA_SQL)
        return st

    async def close(self) -> None:
        await self.pool.close()

    async def version(self) -> str:
        return (await self.pool.fetchval("SELECT version()")).split(",")[0]

    async def cleanup(self) -> None:
        await self.pool.execute("DELETE FROM seen_updates WHERE seen_at < now() - interval '3 days'")

    async def first_seen(self, update_id: int) -> bool:
        """True the first time an update_id arrives; Telegram retries and Render restarts can resend."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)
        row = await self.pool.fetchval(
            "INSERT INTO seen_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING update_id", update_id
        )
        return row is not None

    async def upsert_client(self, chat_id: int, user_id: int | None, username: str | None, full_name: str | None) -> None:
        await self.pool.execute(
            """INSERT INTO clients (chat_id, user_id, username, full_name) VALUES ($1, $2, $3, $4)
               ON CONFLICT (chat_id) DO UPDATE
               SET user_id = $2, username = $3, full_name = $4, last_seen_at = now()""",
            chat_id, user_id, username, full_name,
        )

    async def get_mode(self, chat_id: int) -> str:
        return await self.pool.fetchval("SELECT mode FROM clients WHERE chat_id = $1", chat_id) or "BOT"

    async def set_mode(self, chat_id: int, mode: str) -> None:
        await self.pool.execute(
            "UPDATE clients SET mode = $2, mode_changed_at = now() WHERE chat_id = $1", chat_id, mode
        )

    async def save_message(self, chat_id: int, role: str, text: str, tg_message_id: int | None = None,
                           answered: bool | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO messages (chat_id, role, text, tg_message_id, answered) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            chat_id, role, text, tg_message_id, answered,
        )

    async def mark_answered(self, message_ids: list[int]) -> None:
        if message_ids:
            await self.pool.execute("UPDATE messages SET answered = true WHERE id = ANY($1::bigint[])", message_ids)

    async def history(self, chat_id: int, limit: int = 20) -> list[asyncpg.Record]:
        rows = await self.pool.fetch(
            "SELECT id, role, text, created_at FROM messages WHERE chat_id = $1 ORDER BY id DESC LIMIT $2",
            chat_id, limit,
        )
        return rows[::-1]


# ═══════════════════════════ 8. Sessions ═══════════════════════════

IDLE, COLLECTING, GENERATING, SENDING = "IDLE", "COLLECTING", "GENERATING", "SENDING"
TYPING_EVERY = 4.0  # Telegram shows "typing…" for ~5 s per chat action
SESSION_IDLE_EVICT = 1800.0


class Clock:
    """Injectable time source: tests swap in a fake clock that advances virtually."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


@dataclass
class Incoming:
    chat_id: int
    text: str
    db_id: int | None = None
    tg_id: int = 0  # Telegram message_id: orders a batch when background handlers race


@dataclass
class Reply:
    parts: list[Any]  # opaque to the session; Pipeline.send_part knows how to deliver each one


@dataclass
class SessionConfig:
    debounce: float = 10.0
    max_wait: float = 25.0
    gap_min: float = 1.0
    gap_max: float = 3.0

    @classmethod
    def from_settings(cls, s: Settings) -> SessionConfig:
        return cls(s.debounce_seconds, s.debounce_max_wait, s.part_gap_min, s.part_gap_max)


class Pipeline(Protocol):
    """What a session needs from the rest of the bot (LLM pipeline from Step 5; echo until then)."""

    async def is_human(self, chat_id: int) -> bool: ...
    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None: ...
    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply: ...
    async def send_part(self, chat_id: int, part: Any) -> None: ...
    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[Any]) -> None: ...
    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None: ...


@dataclass
class SessionStats:
    calls: int = 0
    cancels: int = 0
    replies: int = 0


class ChatSession:
    """Per-chat debounce state machine (PLAN.md §6): IDLE → COLLECTING → GENERATING → SENDING."""

    def __init__(self, chat_id: int, pipeline: Pipeline, transport: Transport, cfg: SessionConfig,
                 clock: Clock, rng: random.Random | None = None):
        self.chat_id, self.pipeline, self.transport, self.cfg, self.clock = chat_id, pipeline, transport, cfg, clock
        self.rng = rng or random.Random()
        self.state = IDLE
        self.buffer: list[Incoming] = []  # messages not yet handed to generation
        self.first_at: float | None = None  # arrival of the first buffered message
        self.last_at: float | None = None
        self.batch: list[Incoming] = []  # what the running generation is answering
        self.batch_first_at: float | None = None
        self.last_activity = clock.now()
        self.stats = SessionStats()
        self.lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._gen: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # strong refs so running tasks aren't garbage-collected

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def on_message(self, msg: Incoming) -> None:
        if await self.pipeline.is_human(self.chat_id):  # HUMAN mode: no timer, no LLM
            await self.pipeline.relay_to_human(self.chat_id, [msg])
            return
        async with self.lock:
            now = self.last_activity = self.clock.now()
            self.buffer.append(msg)
            self.last_at = now
            if self.first_at is None:
                self.first_at = now
            if self.state in (IDLE, COLLECTING):
                self.state = COLLECTING
                self._arm()
            elif self.state == GENERATING and now - self.batch_first_at <= self.cfg.max_wait:
                # Nothing sent yet: drop this generation and answer everything together.
                self._gen.cancel()
                self._gen = None
                self.stats.cancels += 1
                self.buffer = self.batch + self.buffer
                self.first_at, self.batch, self.batch_first_at = self.batch_first_at, [], None
                self.state = COLLECTING
                self._arm()
            # GENERATING past MAX_WAIT, or SENDING: the message waits in the buffer for the next batch.

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        deadline = min(self.last_at + self.cfg.debounce, self.first_at + self.cfg.max_wait)
        self._timer = self._spawn(self._fire_at(deadline))

    async def _fire_at(self, deadline: float) -> None:
        await self.clock.sleep(deadline - self.clock.now())
        async with self.lock:
            if self.state != COLLECTING or not self.buffer or self._timer is not asyncio.current_task():
                return
            self._timer = None
            self.batch = sorted(self.buffer, key=lambda m: m.tg_id)
            self.batch_first_at = self.first_at
            self.buffer, self.first_at, self.last_at = [], None, None
            self.state = GENERATING
            self._gen = self._spawn(self._generate_and_send(self.batch))

    async def _generate_and_send(self, batch: list[Incoming]) -> None:
        typing = self._spawn(self._typing_loop())
        try:
            self.stats.calls += 1
            reply = await self.pipeline.generate(self.chat_id, batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("generation failed for chat %s", self.chat_id)
            typing.cancel()
            async with self.lock:
                if self._gen is asyncio.current_task():
                    self._gen, self.state = None, SENDING  # a message now can't cancel the error reply
            try:
                await self.pipeline.on_error(self.chat_id, batch, exc)
            except Exception:
                log.exception("on_error failed for chat %s", self.chat_id)
            await self._finish()
            return
        finally:
            typing.cancel()

        async with self.lock:
            if self._gen is not asyncio.current_task():  # cancelled while waiting for the lock
                return
            self._gen = None
            self.state = SENDING
        sent = await self._send(reply)
        try:
            await self.pipeline.commit(self.chat_id, batch, sent)  # history gets only what was really sent
        except Exception:
            log.exception("commit failed for chat %s", self.chat_id)
        self.stats.replies += 1
        await self._finish()

    async def _typing_loop(self) -> None:
        while True:
            await self.transport.send_typing(self.chat_id)
            await self.clock.sleep(TYPING_EVERY)

    async def _send(self, reply: Reply) -> list[Any]:
        sent = []
        for i, part in enumerate(reply.parts):
            if i:  # the first part goes out at once: the debounce window already made the client wait
                await self.transport.send_typing(self.chat_id)
                await self.clock.sleep(self.rng.uniform(self.cfg.gap_min, self.cfg.gap_max))
            try:
                await self.pipeline.send_part(self.chat_id, part)
                sent.append(part)
            except Exception:
                log.exception("sending part %d failed for chat %s", i, self.chat_id)
        return sent

    async def _finish(self) -> None:
        async with self.lock:
            self.batch, self.batch_first_at = [], None
            self.last_activity = self.clock.now()
            if self.buffer:  # arrived during GENERATING/SENDING: their timer runs from arrival, fires now at the earliest
                self.state = COLLECTING
                self._arm()
            else:
                self.state = IDLE


class SessionManager:
    def __init__(self, pipeline: Pipeline, transport: Transport, cfg: SessionConfig, clock: Clock | None = None):
        self.pipeline, self.transport, self.cfg = pipeline, transport, cfg
        self.clock = clock or Clock()
        self.sessions: dict[int, ChatSession] = {}

    def get(self, chat_id: int) -> ChatSession:
        now = self.clock.now()
        for cid in [c for c, s in self.sessions.items() if s.state == IDLE and now - s.last_activity > SESSION_IDLE_EVICT]:
            del self.sessions[cid]
        if chat_id not in self.sessions:
            self.sessions[chat_id] = ChatSession(chat_id, self.pipeline, self.transport, self.cfg, self.clock)
        return self.sessions[chat_id]

    async def on_message(self, msg: Incoming) -> None:
        await self.get(msg.chat_id).on_message(msg)


class EchoPipeline:
    """Step 4 placeholder for the LLM pipeline (Step 5): proves batching, typing and part gaps live."""

    def __init__(self, storage: Storage, transport: Transport):
        self.storage, self.transport = storage, transport

    async def is_human(self, chat_id: int) -> bool:
        return await self.storage.get_mode(chat_id) == "HUMAN"

    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None:
        log.info("chat %s is in HUMAN mode; relay arrives in Step 6", chat_id)

    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply:
        parts = ["echo: " + " / ".join(m.text for m in batch)]
        if len(batch) > 1:
            parts.append(f"({len(batch)} повідомлення в одному батчі)")
        return Reply(parts)

    async def send_part(self, chat_id: int, part: Any) -> None:
        await self.transport.send_text(chat_id, html.escape(part))

    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[Any]) -> None:
        for part in sent_parts:
            await self.storage.save_message(chat_id, "assistant", part)
        await self.storage.mark_answered([m.db_id for m in batch if m.db_id])

    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None:
        pass


# ═══════════════════════════ 9. Telegram ═══════════════════════════

class Transport(Protocol):
    """Everything the bot sends goes through here; tests pass a FakeTransport that records calls."""

    async def send_text(self, chat_id: int, text: str, *, reply_markup: Any = None, reply_to: int | None = None) -> int: ...
    async def send_photo(self, chat_id: int, file_id: str, caption: str | None = None) -> int: ...
    async def send_media_group(self, chat_id: int, file_ids: list[str], caption: str | None = None) -> list[int]: ...
    async def send_typing(self, chat_id: int) -> None: ...
    async def copy_message(self, to_chat_id: int, from_chat_id: int, message_id: int, reply_to: int | None = None) -> int: ...


class BotTransport:
    """Real Telegram via aiogram. Texts and captions are HTML; link previews are off."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _call(self, fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            return await fn(*args, **kwargs)
        except TelegramForbiddenError:
            log.warning("chat %s blocked the bot", args[0] if args else kwargs.get("chat_id"))
            return None

    async def send_text(self, chat_id, text, *, reply_markup=None, reply_to=None) -> int:
        m = await self._call(self.bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=reply_markup,
                             reply_to_message_id=reply_to, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return m.message_id if m else 0

    async def send_photo(self, chat_id, file_id, caption=None) -> int:
        m = await self._call(self.bot.send_photo, chat_id, file_id, caption=caption, parse_mode="HTML")
        return m.message_id if m else 0

    async def send_media_group(self, chat_id, file_ids, caption=None) -> list[int]:
        media = [InputMediaPhoto(media=f, caption=caption if i == 0 else None, parse_mode="HTML")
                 for i, f in enumerate(file_ids)]
        msgs = await self._call(self.bot.send_media_group, chat_id, media)
        return [m.message_id for m in msgs or []]

    async def send_typing(self, chat_id) -> None:
        await self._call(self.bot.send_chat_action, chat_id, "typing")

    async def copy_message(self, to_chat_id, from_chat_id, message_id, reply_to=None) -> int:
        m = await self._call(self.bot.copy_message, to_chat_id, from_chat_id, message_id, reply_to_message_id=reply_to)
        return m.message_id if m else 0


@dataclass
class Runtime:
    """Everything handlers need; put into the dispatcher as dp["rt"]."""

    settings: Settings
    storage: Storage
    transport: Transport
    sessions: SessionManager
    extras: dict = field(default_factory=dict)

    def allowed(self, user_id: int) -> bool:
        return not self.settings.allowed_user_ids or user_id in self.settings.allowed_user_ids


async def build_runtime(settings: Settings, bot: Bot) -> Runtime:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is empty")
    storage = await Storage.connect(settings.database_url)
    await storage.cleanup()
    log.info("database ok: %s", await storage.version())
    transport = BotTransport(bot)
    sessions = SessionManager(EchoPipeline(storage, transport), transport, SessionConfig.from_settings(settings))
    return Runtime(settings, storage, transport, sessions)


router = Router()


@router.message(F.chat.type == "private", F.text)
async def on_private_text(message: Message, event_update: Update, rt: Runtime) -> None:
    user = message.from_user
    if not rt.allowed(user.id) or not await rt.storage.first_seen(event_update.update_id):
        return
    await rt.storage.upsert_client(message.chat.id, user.id, user.username, user.full_name)
    db_id = await rt.storage.save_message(message.chat.id, "user", message.text, message.message_id, answered=False)
    await rt.sessions.on_message(Incoming(message.chat.id, message.text, db_id, message.message_id))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


# ═══════════════════════════ 10. App ═══════════════════════════

class AccessLogger(AbstractAccessLogger):
    """Masks the webhook secret in the path and skips health-check noise (Render + UptimeRobot)."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if request.path == "/healthz":
            return
        path = "/tg/***" if request.path.startswith("/tg/") else request.path
        self.logger.info("%s %s %s %.0fms", request.method, path, response.status, time * 1000)


async def healthz(request: web.Request) -> web.Response:
    # Never touch the DB here: UptimeRobot hits this every 5 min and Neon must be able to sleep.
    return web.Response(text="ok")


def build_app(settings: Settings, bot: Bot, dp: Dispatcher) -> web.Application:
    if not settings.webhook_base_url:
        raise RuntimeError("PUBLIC_URL / RENDER_EXTERNAL_URL is empty; use --polling for local runs")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    # Checks X-Telegram-Bot-Api-Secret-Token and answers 200 right away (handling runs in background).
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.webhook_secret).register(
        app, path=settings.webhook_path
    )

    async def on_startup(app: web.Application) -> None:
        dp["rt"] = await build_runtime(settings, bot)
        await bot.set_webhook(
            settings.webhook_base_url + settings.webhook_path,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("webhook set: %s/tg/***", settings.webhook_base_url)

    async def on_cleanup(app: web.Application) -> None:
        if "rt" in dp.workflow_data:
            await dp["rt"].storage.close()
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def run_polling(settings: Settings, bot: Bot, dp: Dispatcher) -> None:
    rt = dp["rt"] = await build_runtime(settings, bot)
    try:
        await bot.delete_webhook()
        log.info("webhook deleted, polling. To hand back to Render: python scrape.py webhook set")
        await dp.start_polling(bot)
    finally:
        await rt.storage.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--polling", action="store_true", help="local dev: delete the webhook and long-poll")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    bot = Bot(settings.telegram_bot_token)
    dp = build_dispatcher()
    if args.polling:
        try:
            asyncio.run(run_polling(settings, bot, dp))
        except KeyboardInterrupt:  # Ctrl+C on Windows surfaces here
            log.info("stopped")
    else:
        web.run_app(
            build_app(settings, bot, dp), host="0.0.0.0", port=settings.port, access_log_class=AccessLogger
        )


if __name__ == "__main__":
    main()
