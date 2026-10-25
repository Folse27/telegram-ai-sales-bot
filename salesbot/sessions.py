"""Per-chat debounce: wait until the client stops typing, answer a burst once, never interleave two replies.

IDLE → COLLECTING (timer runs) → GENERATING (model call) → SENDING (parts with short pauses) → IDLE
A message during GENERATING cancels the draft and joins the batch (until the max wait); one during SENDING
waits for the next batch.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings
from .transport import Transport

log = logging.getLogger("salesbot")

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
    extra: dict = field(default_factory=dict)  # pipeline data for commit (order plan, escalation)


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
    """What a session needs from the rest of the bot."""

    async def is_human(self, chat_id: int) -> bool: ...
    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None: ...
    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply: ...
    async def send_part(self, chat_id: int, part: Any) -> None: ...
    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[Any], reply: Reply) -> None: ...
    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None: ...


@dataclass
class SessionStats:
    calls: int = 0
    cancels: int = 0
    replies: int = 0


class ChatSession:
    def __init__(self, chat_id: int, pipeline: Pipeline, transport: Transport, cfg: SessionConfig,
                 clock: Clock, rng: random.Random | None = None):
        self.chat_id, self.pipeline, self.transport, self.cfg, self.clock = chat_id, pipeline, transport, cfg, clock
        self.rng = rng or random.Random()  # noqa: S311 - pauses between messages, not security
        self.state = IDLE
        self.buffer: list[Incoming] = []  # messages not yet handed to generation
        self.first_at: float | None = None  # arrival of the first buffered message
        self.last_at: float | None = None
        self.batch: list[Incoming] = []  # what the running generation is answering
        self.batch_first_at: float | None = None
        self.batch_last_at: float | None = None  # arrival of the batch's last message (latency measurement)
        self.last_activity = clock.now()
        self.stats = SessionStats()
        self.lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._gen: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # strong refs so running tasks aren't garbage-collected

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def on_message(self, msg: Incoming) -> None:
        if await self.pipeline.is_human(self.chat_id):  # HUMAN mode: no timer, no model call
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
            elif (self.state == GENERATING and self._gen and self.batch_first_at is not None
                  and now - self.batch_first_at <= self.cfg.max_wait):
                # Nothing sent yet: drop this generation and answer everything together.
                self._gen.cancel()
                self._gen = None
                self.stats.cancels += 1
                self.buffer = self.batch + self.buffer
                self.first_at, self.batch, self.batch_first_at = self.batch_first_at, [], None
                self.state = COLLECTING
                self._arm()
            # GENERATING past the max wait, or SENDING: the message waits in the buffer for the next batch.

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        assert self.last_at is not None and self.first_at is not None
        deadline = min(self.last_at + self.cfg.debounce, self.first_at + self.cfg.max_wait)
        self._timer = self._spawn(self._fire_at(deadline))

    async def _fire_at(self, deadline: float) -> None:
        await self.clock.sleep(deadline - self.clock.now())
        async with self.lock:
            if self.state != COLLECTING or not self.buffer or self._timer is not asyncio.current_task():
                return
            self._timer = None
            self.batch = sorted(self.buffer, key=lambda m: m.tg_id)
            self.batch_first_at, self.batch_last_at = self.first_at, self.last_at
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
            await self.pipeline.commit(self.chat_id, batch, sent, reply)  # history gets only what was really sent
        except Exception:
            log.exception("commit failed for chat %s", self.chat_id)
        self.stats.replies += 1
        await self._finish()

    async def _typing_loop(self) -> None:
        while True:
            await self.transport.send_typing(self.chat_id)
            await self.clock.sleep(TYPING_EVERY)

    async def _send(self, reply: Reply) -> list[Any]:
        sent: list[Any] = []
        for i, part in enumerate(reply.parts):
            if i:  # the first part goes out at once: the debounce window already made the client wait
                await self.transport.send_typing(self.chat_id)
                await self.clock.sleep(self.rng.uniform(self.cfg.gap_min, self.cfg.gap_max))
            try:
                await self.pipeline.send_part(self.chat_id, part)
                if not sent and self.batch_last_at is not None:  # client's last message → first part (/stats)
                    reply.extra["first_part_after_s"] = round(self.clock.now() - self.batch_last_at, 2)
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

    def drop(self, chat_id: int) -> None:
        """Forget a chat's in-memory state (buffer, timers, a running generation)."""
        session = self.sessions.pop(chat_id, None)
        if session:
            for task in list(session._tasks):
                task.cancel()
