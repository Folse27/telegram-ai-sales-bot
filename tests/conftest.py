"""Shared fakes: a virtual clock, a recording Telegram transport and a scripted pipeline."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import Incoming, Reply  # noqa: E402


async def settle(rounds: int = 50) -> None:
    """Let every ready task run until the loop is quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Virtual time: sleep() parks until advance() moves the clock past the wake time."""

    def __init__(self):
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

    def __init__(self):
        self.calls: list[tuple] = []
        self._ids = itertools.count(1000)

    def of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]

    async def send_text(self, chat_id, text, *, reply_markup=None, reply_to=None) -> int:
        self.calls.append(("text", chat_id, text, reply_markup, reply_to))
        return next(self._ids)

    async def send_photo(self, chat_id, file_id, caption=None) -> int:
        self.calls.append(("photo", chat_id, file_id, caption))
        return next(self._ids)

    async def send_media_group(self, chat_id, file_ids, caption=None) -> list[int]:
        self.calls.append(("media_group", chat_id, list(file_ids), caption))
        return [next(self._ids) for _ in file_ids]

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
