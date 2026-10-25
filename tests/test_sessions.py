"""Debounce engine on a virtual clock: no network, no database."""
from __future__ import annotations

import itertools
import random

import pytest
from conftest import FakeClock, FakePipeline, FakeTransport

from salesbot.sessions import IDLE, SENDING, Incoming, SessionConfig, SessionManager

CHAT = 1


def setup(gen_time: float = 2.0, parts: int = 1, human: bool = False):
    clock = FakeClock()
    transport = FakeTransport()
    pipeline = FakePipeline(clock, gen_time=gen_time, parts=parts, human=human)
    sessions = SessionManager(pipeline, transport, SessionConfig(debounce=10, max_wait=25, gap_min=1, gap_max=3), clock)
    return clock, transport, pipeline, sessions


async def say(sessions: SessionManager, text: str, n: int) -> None:
    await sessions.on_message(Incoming(CHAT, text, db_id=n, tg_id=n))


async def test_single_message_one_call_after_debounce():
    clock, _, pl, sm = setup()
    await say(sm, "привіт", 1)
    await clock.advance(9.9)
    assert pl.calls == []
    await clock.advance(0.2)
    assert pl.calls == [["привіт"]] and pl.call_times == [10.0]
    await clock.advance(5)
    assert pl.committed == [(["привіт"], ["привіт#0"])]
    assert sm.get(CHAT).state == IDLE


async def test_four_messages_two_seconds_apart_are_one_call():
    clock, _, pl, sm = setup()
    for i, text in enumerate(["Привіт", "цікавить детектор дронів", "щоб в авто стояв", "скільки з автотримачем?"]):
        if i:
            await clock.advance(2)
        await say(sm, text, i + 1)
    await clock.advance(9.9)  # t = 15.9, last message at 6
    assert pl.calls == []
    await clock.advance(10)
    assert len(pl.calls) == 1 and len(pl.calls[0]) == 4 and pl.call_times == [16.0]
    assert len(pl.committed) == 1


async def test_message_during_generation_cancels_and_merges():
    clock, _, pl, sm = setup(gen_time=2)
    await say(sm, "скільки коштує Щит 4М?", 1)
    await clock.advance(11)  # generation started at 10, runs until 12
    await say(sm, "і з якими антенами він іде?", 2)
    await clock.advance(30)
    s = sm.get(CHAT)
    assert s.stats.cancels == 1 and s.stats.replies == 1
    assert pl.calls[-1] == ["скільки коштує Щит 4М?", "і з якими антенами він іде?"]
    assert pl.committed == [(pl.calls[-1], ["скільки коштує Щит 4М?+і з якими антенами він іде?#0"])]
    assert len(pl.sent) == 1  # the cancelled generation sent nothing


async def test_message_during_sending_gets_second_reply_in_order():
    clock, _, pl, sm = setup(gen_time=2, parts=2)
    await say(sm, "a", 1)
    await clock.advance(12)  # fire at 10, reply ready at 12, first part sent, gap before part 2
    assert sm.get(CHAT).state == SENDING and len(pl.sent) == 1
    await clock.advance(0.5)
    await say(sm, "b", 2)
    await clock.advance(40)
    assert [c[0] for c in pl.committed] == [["a"], ["b"]]
    assert [p for _, p in pl.sent] == ["a#0", "a#1", "b#0", "b#1"]
    assert pl.call_times[1] == pytest.approx(22.5)  # b's own 10 s window from its arrival at 12.5
    assert sm.get(CHAT).stats.cancels == 0


async def test_max_wait_caps_a_long_stream():
    clock, _, pl, sm = setup()
    for i in range(6):  # a message every 5 s never lets the 10 s window close
        await say(sm, f"m{i}", i + 1)
        await clock.advance(5)
    assert pl.call_times[0] == 25.0 and len(pl.calls[0]) == 5


async def test_message_after_max_wait_during_generation_is_queued_not_cancelled():
    clock, _, pl, sm = setup(gen_time=5)
    for i in range(5):  # messages at 0..20, fire at 25 (max wait), generating until 30
        await say(sm, f"m{i}", i + 1)
        await clock.advance(5)
    await clock.advance(2)  # t = 27: GENERATING, 27 s since the batch's first message
    await say(sm, "late", 99)
    await clock.advance(60)
    s = sm.get(CHAT)
    assert s.stats.cancels == 0 and s.stats.replies == 2
    assert pl.calls[1] == ["late"] and pl.call_times[1] == 37.0


async def test_human_mode_makes_no_llm_calls():
    clock, tr, pl, sm = setup(human=True)
    for i in range(3):
        await say(sm, f"m{i}", i + 1)
    await clock.advance(120)
    assert pl.calls == [] and pl.relayed == ["m0", "m1", "m2"] and tr.of("typing") == []


async def test_typing_is_sent_while_generating():
    clock, tr, _, sm = setup(gen_time=9)
    await say(sm, "питання", 1)
    await clock.advance(10.5)
    assert len(tr.of("typing")) == 1
    await clock.advance(8)  # typing again at 14 and 18
    assert len(tr.of("typing")) == 3


async def test_first_part_immediately_then_random_gaps():
    clock, _, pl, sm = setup(gen_time=2, parts=3)
    sm.get(CHAT).rng = random.Random(7)  # noqa: S311 - deterministic pauses for the test
    await say(sm, "q", 1)
    await clock.advance(30)
    times = [t for t, _ in pl.sent]
    assert times[0] == 12.0
    assert all(1 <= b - a <= 3 for a, b in itertools.pairwise(times))


async def test_generation_error_goes_to_on_error_and_session_recovers():
    clock, _, pl, sm = setup()
    pl.fail_next = True
    await say(sm, "перше", 1)
    await clock.advance(15)
    assert pl.errors == [["перше"]] and pl.committed == [] and sm.get(CHAT).state == IDLE
    await say(sm, "друге", 2)
    await clock.advance(15)
    assert pl.committed == [(["друге"], ["друге#0"])]


async def test_batch_is_ordered_by_telegram_message_id():
    clock, _, pl, sm = setup()
    await say(sm, "second", 2)  # background handlers can finish out of order
    await say(sm, "first", 1)
    await clock.advance(11)
    assert pl.calls == [["first", "second"]]


async def test_idle_sessions_are_evicted_after_30_minutes():
    clock, _, _, sm = setup()
    await say(sm, "q", 1)
    await clock.advance(20)
    first = sm.get(CHAT)
    await clock.advance(1801)
    sm.get(2)  # any access sweeps idle sessions
    assert CHAT not in sm.sessions
    assert sm.get(CHAT) is not first


async def test_session_drop_cancels_its_tasks():
    clock, _, pl, sm = setup()
    await say(sm, "q", 1)
    sm.drop(CHAT)
    await clock.advance(30)
    assert pl.calls == [] and CHAT not in sm.sessions


async def test_first_part_latency_is_measured_from_the_last_client_message():
    clock, _, pl, sm = setup()
    await say(sm, "привіт", 1)
    await clock.advance(3)
    await say(sm, "є чохли?", 2)  # last message at t=3 → call at 13, first part at 15
    await clock.advance(20)
    assert pl.extras[-1]["first_part_after_s"] == 12.0
