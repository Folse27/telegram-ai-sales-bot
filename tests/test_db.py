"""Tests against a real Postgres (TEST_DATABASE_URL; each test gets a throwaway schema)."""
from __future__ import annotations

from types import SimpleNamespace

import asyncpg
import pytest
from conftest import ADMIN, CAT, ORD, TPL, VAL, FakeTransport, settings

from salesbot.config import SAMPLE_SHOP_DIR
from salesbot.handoff import Handoff
from salesbot.photos import PhotoCache
from salesbot.pipeline import LlmPipeline
from salesbot.sessions import Incoming, Reply
from salesbot.shop import ShopData, ShopDataError
from salesbot.storage import SCHEMA_SQL

pytestmark = pytest.mark.db


def _handoff(storage, tr=None, **extra):
    return Handoff(SimpleNamespace(admin_chat_id=ADMIN, cache_ttl="1h", **extra), storage, tr or FakeTransport(), TPL, ORD)


async def _fake_fetch(src: str) -> bytes | None:
    return None if "missing" in src else b"image-bytes"


def _pipeline(storage, tr, ho, **overrides):
    photos = PhotoCache(storage, fetch=_fake_fetch)
    return LlmPipeline(settings(**overrides), storage, tr, CAT, TPL, VAL, None, ORD, ho, photos)  # type: ignore[arg-type]


async def test_storage_schema_and_basic_queries(storage):
    # every pooled connection must live in the throwaway schema, never in public (Neon ignores startup search_path)
    schemas = {await storage.pool.fetchval("SELECT current_schema()") for _ in range(3)}
    assert len(schemas) == 1 and schemas.pop().startswith("t_")
    await storage.pool.execute(SCHEMA_SQL)  # creating tables twice is harmless
    tables = {r["table_name"] for r in await storage.pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert {"clients", "seen_updates", "messages", "orders", "handoffs", "relay_map", "llm_calls", "shop_files",
            "photo_cache"} <= tables

    assert await storage.first_seen(555) is True
    assert await storage.first_seen(555) is False  # in-memory LRU
    storage._seen.clear()
    assert await storage.first_seen(555) is False  # database table (e.g. after a restart)

    await storage.upsert_client(42, 42, "tester", "Тест Тестович")
    assert await storage.get_mode(42) == "BOT"
    await storage.set_mode(42, "HUMAN")
    assert await storage.get_mode(42) == "HUMAN"

    m1 = await storage.save_message(42, "user", "скільки коштує Сенсор S3?", 10, answered=False)
    await storage.save_message(42, "assistant", "24 000 грн з ПДВ")
    await storage.mark_answered([m1])
    hist = await storage.history(42)
    assert [(r["role"], r["text"]) for r in hist] == [("user", "скільки коштує Сенсор S3?"),
                                                      ("assistant", "24 000 грн з ПДВ")]
    assert await storage.pool.fetchval("SELECT answered FROM messages WHERE id = $1", m1) is True

    await storage.pool.execute("INSERT INTO orders (chat_id, status, data) VALUES (42, 'DRAFT', $1)", {"items": [1]})
    assert (await storage.pool.fetchval("SELECT data FROM orders WHERE chat_id = 42")) == {"items": [1]}
    with pytest.raises(asyncpg.UniqueViolationError):  # only one editable order per chat
        await storage.pool.execute("INSERT INTO orders (chat_id, status) VALUES (42, 'AWAITING_CONFIRMATION')")


async def test_handoff_card_relay_buttons_and_expiry(storage):
    tr = FakeTransport()
    ho = _handoff(storage, tr)
    await storage.upsert_client(42, 42, "petro", "Петро")
    await storage.save_message(42, "user", "Покличте менеджера", 7, answered=False)

    await ho.open(42, "client_request", "sales", "Клієнт просить менеджера", human=True)
    card = tr.of("text")[-1]
    assert card[1] == ADMIN and "Запит #" in card[2] and "Покличте менеджера" in card[2] and card[3] is not None
    assert await ho.is_human(42)
    card_id = await storage.pool.fetchval("SELECT card_message_id FROM handoffs WHERE chat_id = 42")

    await ho.open(42, "unknown_fact", "sales", "Ще питання", human=True)  # a second request extends the same card
    assert "Ще запит" in tr.of("text")[-1][2] and tr.of("text")[-1][4] == card_id
    assert await storage.pool.fetchval("SELECT count(*) FROM handoffs WHERE chat_id = 42") == 1

    await ho.relay_client(42, 8)  # the client's message is copied under the card
    assert tr.of("copy")[-1] == ("copy", ADMIN, 42, 8, card_id)
    assert await ho.manager_reply(card_id, 555, "Добрий день, це Олег") == 42
    assert tr.of("copy")[-1] == ("copy", 42, ADMIN, 555, None)
    assert (await storage.history(42))[-1]["role"] == "manager"
    assert await ho.manager_reply(999999, 556, "?") is None  # unmapped: ignored

    hid = await storage.pool.fetchval("SELECT id FROM handoffs WHERE chat_id = 42")
    assert "Бот знову" in await ho.button(hid, "bot", "Олег")
    assert not await ho.is_human(42) and tr.of("edit_markup")[-1][2] == card_id

    await storage.set_mode(42, "HUMAN")  # 12 h without manager activity → back to the bot on the next message
    await storage.pool.execute("UPDATE clients SET mode_changed_at = now() - interval '13 hours',"
                               " last_manager_at = now() - interval '13 hours' WHERE chat_id = 42")
    assert not await ho.is_human(42)


async def test_admin_commands(storage):
    tr = FakeTransport()
    ho = _handoff(storage, tr)
    await storage.upsert_client(43, 43, None, "Олена")
    oid = await storage.pool.fetchval(
        "INSERT INTO orders (chat_id, status, data) VALUES (43, 'PAYMENT_SENT', $1) RETURNING id",
        {"items": [], "totals": {"with_vat": 24480}, "customer": {}, "vat_mode": "with_vat"})
    assert "№" in await ho.command("/orders")
    assert "✅" in await ho.command(f"/paid@SampleShopBot {oid}")
    assert await storage.pool.fetchval("SELECT status FROM orders WHERE id = $1", oid) == "PAID"
    assert tr.of("text")[-1][1] == 43 and "Оплату отримано" in tr.of("text")[-1][2]
    assert "уже PAID" in await ho.command(f"/cancel {oid}")
    assert "Формат" in await ho.command("/paid abc")


async def test_resume_mapping_and_wipe(storage):
    tr = FakeTransport()
    ho = _handoff(storage, tr)
    resumed = []

    async def on_resume(chat_id):
        resumed.append((chat_id, [r["text"] for r in await ho.trailing_client_messages(chat_id)]))

    ho.on_resume = on_resume
    await storage.upsert_client(44, 44, None, "Тест")
    await storage.save_message(44, "user", "покличте менеджера", 1, answered=False)
    await ho.open(44, "client_request", "sales", "Клієнт просить менеджера", human=True)
    hid = await storage.pool.fetchval("SELECT id FROM handoffs WHERE chat_id = 44")
    await ho.button(hid, "take", "Олег")
    take_notice = await storage.pool.fetchval(
        "SELECT max(admin_message_id) FROM relay_map WHERE chat_id = 44")  # the "✋ взяв" notice is mapped too
    assert await ho.manager_reply(take_notice, 777, "Вітаю, з вами Сергій") == 44
    await storage.save_message(44, "user", "Можете підсказати, як проходить оплата на ФОП?", 2, answered=True)
    await ho.button(hid, "bot", "Олег")
    assert resumed == [(44, ["Можете підсказати, як проходить оплата на ФОП?"])]  # answered after the hand-back

    await ho.notify_order("payment_sent", 1, 44)  # replies to order notifications reach the client as well
    assert tr.of("text")[-1][1] == ADMIN
    assert await storage.pool.fetchval("SELECT chat_id FROM relay_map WHERE admin_message_id = $1",
                                       await storage.pool.fetchval("SELECT max(admin_message_id) FROM relay_map")) == 44

    await storage.wipe_chat(44)
    for table in ("messages", "orders", "handoffs", "relay_map"):
        assert await storage.pool.fetchval(f"SELECT count(*) FROM {table} WHERE chat_id = 44") == 0
    assert await storage.get_mode(44) == "BOT"


async def test_handoff_ack_only_when_nothing_promised_a_follow_up(storage):
    tr = FakeTransport()
    ho = _handoff(storage, tr)
    pl = _pipeline(storage, tr, ho)
    esc = {"needed": True, "reason": "vat_eligibility", "department": "sales", "summary_uk": "без ПДВ"}
    for chat in (45, 46):
        await storage.upsert_client(chat, chat, None, "Тест")
    vat = TPL.render({"template_id": "vat_explainer", "product_slug": "sensor-s3"})  # ends with «я уточню…»
    await pl.commit(45, [], [vat], Reply([vat], {"escalate": esc}))
    assert not [c for c in tr.calls if c[1] == 45]  # no second promise to the client
    card = TPL.render({"template_id": "product_card", "product_slug": "sensor-s3"})  # no promise in it
    await pl.commit(46, [], [card], Reply([card], {"escalate": esc}))
    assert [c[2] for c in tr.of("text") if c[1] == 46] == [TPL.text("handoff_ack").text]


async def test_stats_command(storage):
    ho = _handoff(storage)
    assert "даних ще немає" in await ho.command("/stats")
    await storage.upsert_client(47, 47, None, "Тест")
    usage = SimpleNamespace(input_tokens=500, output_tokens=300, cache_read_input_tokens=15000,
                            cache_creation_input_tokens=0)
    await storage.log_llm_call(47, "claude-sonnet-5-5", 1, usage, 4000, "invalid", None)
    await storage.log_llm_call(47, "claude-sonnet-5-5", 2, usage, 6000, "ok", None)
    for s in (11.5, 14.0, 20.0):
        await storage.save_message(47, "assistant", "x", meta={"first_part_after_s": s})
    await storage.pool.execute("INSERT INTO orders (chat_id, status) VALUES (47, 'PAID')")
    out = await ho.command("/stats")
    assert "Відповідей: 1" in out and "відхилено валідатором: 1" in out
    assert "p50 14.0 с · p90 20.0 с (3 відп.)" in out and "PAID 1" in out and "Кеш: прочитано 97%" in out
    assert "за весь час" in await ho.command("/stats 0")


async def test_photo_cache_uploads_once_then_reuses_the_file_id(storage):
    tr = FakeTransport()
    cache = PhotoCache(storage, fetch=_fake_fetch)
    srcs = ["data/sample/images/sensor-s3-0.png", "data/sample/images/sensor-s3-1.png"]
    assert await cache.send(tr, 1, srcs, "Сенсор S3")
    assert tr.of("photos")[-1][2] == [f"upload:{s}" for s in srcs]  # first time: uploaded
    assert await cache.send(tr, 2, srcs, "Сенсор S3")
    assert tr.of("photos")[-1][2] == [f"fid:{s}" for s in srcs]  # then: Telegram's file_ids
    assert (await PhotoCache(storage).load()).ids == {s: f"fid:{s}" for s in srcs}  # survives a restart

    tr.reject_file_ids = True  # stale file_ids (e.g. a new bot token): forgotten and uploaded again once
    assert await cache.send(tr, 3, srcs, "Сенсор S3")
    assert tr.of("photos")[-1][2] == [f"upload:{s}" for s in srcs]

    assert not await cache.send(tr, 4, ["missing.png"], "x")  # nothing readable → caller sends text instead
    n = await cache.import_legacy({"k": {"file_id": "old", "src": "a.png"}, "z": {"file_id": "x", "src": "gone.png"}},
                                  ["a.png"])
    assert n == 1 and cache.ids["a.png"] == "old"


async def test_photo_part_falls_back_to_its_caption(storage):
    tr = FakeTransport()
    pl = _pipeline(storage, tr, _handoff(storage, tr))
    pl.photos.ids.clear()
    part = TPL.render({"template_id": "product_card", "product_slug": "sensor-s3"})
    part.photos = ["missing-1.png"]
    await pl.send_part(9, part)
    assert tr.of("text")[-1][2] == part.text and not tr.of("photos")


async def test_shop_data_round_trip_through_the_database(storage):
    with pytest.raises(ShopDataError, match="no shop data"):
        await ShopData.from_db(storage)
    texts = ShopData.read_folder(SAMPLE_SHOP_DIR)
    await ShopData.upload(storage, texts)
    await ShopData.upload(storage, texts)  # replacing is idempotent
    shop = await ShopData.from_db(storage)
    assert shop.source == "database" and len(shop.catalog["products"]) == len(CAT.products)
    assert shop.content == ShopData.from_folder(SAMPLE_SHOP_DIR).content


async def test_usage_limits(storage):
    tr = FakeTransport()
    ho = _handoff(storage, tr)
    await storage.upsert_client(48, 48, None, "Тест")
    ok = SimpleNamespace(input_tokens=100, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    for _ in range(2):
        await storage.log_llm_call(48, "claude-haiku-4-5", 1, ok, 1000, "ok", None)

    capped = _pipeline(storage, tr, ho, daily_replies_per_user=2, daily_budget_usd=0)
    assert await capped.over_limit(48) == "user" and await capped.over_limit(49) is None
    assert await _pipeline(storage, tr, ho, daily_replies_per_user=2, daily_budget_usd=0,
                           tester_ids=[48]).over_limit(48) is None  # testers are exempt from the per-user cap

    first = await capped.generate(48, [Incoming(48, "ще питання")])  # no model call (llm is None here)
    assert [p.template_id for p in first.parts] == ["limit_reached"]
    await capped.commit(48, [], first.parts, first)
    assert (await capped.generate(48, [Incoming(48, "ало?")])).parts == []  # said once, then quiet

    broke = _pipeline(storage, tr, ho, daily_replies_per_user=0, daily_budget_usd=0.0001)
    assert await broke.over_limit(50) == "budget" and await broke.over_limit(51) == "budget"
    assert sum("бюджет" in c[2] for c in tr.of("text") if c[1] == ADMIN) == 1  # staff told once, not per message
