"""Unit tests. Debounce tests run on a virtual clock (no network). Storage tests need TEST_DATABASE_URL."""
from __future__ import annotations

import json
import random
import re
import uuid
from types import SimpleNamespace

import pytest
from conftest import FakeClock, FakePipeline, FakeTransport

from main import (IDLE, SENDING, Catalog, Incoming, SessionConfig, SessionManager, Storage, Templates, Validator,
                  build_system, clean_site_text, reference_numbers)

CHAT = 1


def setup(gen_time: float = 2.0, parts: int = 1, human: bool = False):
    clock = FakeClock()
    transport = FakeTransport()
    pipeline = FakePipeline(clock, gen_time=gen_time, parts=parts, human=human)
    sessions = SessionManager(pipeline, transport, SessionConfig(debounce=10, max_wait=25, gap_min=1, gap_max=3), clock)
    return clock, transport, pipeline, sessions


async def say(sessions: SessionManager, text: str, n: int) -> None:
    await sessions.on_message(Incoming(CHAT, text, db_id=n, tg_id=n))


# ═══════════════════════════ debounce (PLAN.md §6) ═══════════════════════════

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
    for i in range(5):  # messages at 0..20, fire at 25 (MAX_WAIT), generating until 30
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
    sm.get(CHAT).rng = random.Random(7)
    await say(sm, "q", 1)
    await clock.advance(30)
    times = [t for t, _ in pl.sent]
    assert times[0] == 12.0
    assert all(1 <= b - a <= 3 for a, b in zip(times, times[1:]))


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


# ═══════════════════════════ storage (Neon test DB) ═══════════════════════════

def _test_dsn() -> str | None:
    try:
        from config import get_settings
        return get_settings().test_database_url or None
    except Exception:  # no .env on this machine
        return None


@pytest.fixture
async def storage():
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = f"t_{uuid.uuid4().hex[:10]}"
    st = await Storage.connect(dsn, schema=schema)
    yield st
    await st.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await st.close()


@pytest.mark.db
async def test_storage_schema_and_basic_queries(storage):
    # every pooled connection must live in the throwaway schema, never in public (Neon ignores startup search_path)
    schemas = {await storage.pool.fetchval("SELECT current_schema()") for _ in range(3)}
    assert len(schemas) == 1 and schemas.pop().startswith("t_")
    await storage.pool.execute(__import__("main").SCHEMA_SQL)  # creating tables twice is harmless
    tables = {r["table_name"] for r in await storage.pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert {"clients", "seen_updates", "messages", "orders", "handoffs", "relay_map", "llm_calls"} <= tables

    assert await storage.first_seen(555) is True
    assert await storage.first_seen(555) is False  # in-memory LRU
    storage._seen.clear()
    assert await storage.first_seen(555) is False  # DB table (e.g. after a restart)

    await storage.upsert_client(42, 42, "tester", "Тест Тестович")
    assert await storage.get_mode(42) == "BOT"
    await storage.set_mode(42, "HUMAN")
    assert await storage.get_mode(42) == "HUMAN"

    m1 = await storage.save_message(42, "user", "скільки коштує Сенсора 3.0?", 10, answered=False)
    await storage.save_message(42, "assistant", "30 840 грн з ПДВ")
    await storage.mark_answered([m1])
    hist = await storage.history(42)
    assert [(r["role"], r["text"]) for r in hist] == [("user", "скільки коштує Сенсора 3.0?"), ("assistant", "30 840 грн з ПДВ")]
    assert await storage.pool.fetchval("SELECT answered FROM messages WHERE id = $1", m1) is True

    await storage.pool.execute("INSERT INTO orders (chat_id, status, data) VALUES (42, 'DRAFT', $1)", {"items": [1]})
    assert (await storage.pool.fetchval("SELECT data FROM orders WHERE chat_id = 42")) == {"items": [1]}
    with pytest.raises(Exception):  # only one editable order per chat
        await storage.pool.execute("INSERT INTO orders (chat_id, status) VALUES (42, 'AWAITING_CONFIRMATION')")


# ═══════════════════════════ catalog / retrieval / templates / validators (Step 5) ═══════════════════════════

CAT = Catalog.load()
CONTENT = Templates.load_content()
TPL = Templates(CONTENT, CAT, "Микола")
VAL = Validator(CAT, CONTENT)
PERSONA = SimpleNamespace(persona_name="Микола")
ALLOWED = reference_numbers(build_system(PERSONA, CAT, CONTENT)) | {"1"}

RETRIEVAL_CASES = [
    ("Скільки коштує Сенсора 3.0?", "sensor-3-0"),
    ("а сенсору 4.0 маєте?", "sensor-4-0"),
    ("sensor 3.0 price", "sensor-3-0"),
    ("цікавить детектор дронів, щоб в авто стояв, скільки з автотримачем?", "sensor-3-0"),
    ("автотримач для сенсори окремо продаєте?", "universalnij-avtomobilnij-trimach-dlya-prer-sensor"),
    ("Скільки коштує Щит 4М?", "reb-shchyt-4m"),
    ("can we buy Shield 4M for export", "reb-shchyt-4m"),
    ("щит 6м потужний?", "reb-shchyt-6m"),
    ("модульний щит хл", "modulnyj-reb-shchyt-xl"),
    ("глушник Щит 4G", "pridushuvach-signaliv-shchyt-4g"),
    ("сколько стоит антенна Clover 5.8", "antena-clover-5-8-ghz"),
    ("кловер 3.5-4.5", "antena-clover-35-45-ggcz"),
    ("хелікс лайт 5.8/18", "antena-helix-lite-5-8-18-vid-obrii"),
    ("HELIX Lite 5.8/12 є?", "antena-helix-lite-5-8-12-ggcz-vid-obrii"),
    ("Гарпуна 2.4", "spryamovana-antena-garpun-2-4-ggcz"),
    ("секторна антена 5.2", "sektorna-antena-5-2-ggcz-5150-5300-mgcz-vid-obrii"),
    ("квадрифіляр 4 в 1", "anteni-kvadrofilyar-4-chastoti"),
    ("MOXON 430", "moxon-r-430-mhz-antena"),
    ("Yagi-Uda 915", "antena-uya-lite-915-vid-obrii"),
    ("зарядна станція Енергія 1200", "akumulyator-obrii-dlya-reb-220-v"),
    ("Енергія-28 скільки коштує", "akumulyator-obrii-dlya-reb"),
    ("наземний дрон тягача", "nazemnyj-robotyzovanyj-kompleks-tiahach-vid-obrii"),
    ("тягача мінер", "nazemnij-dron-tiahach-miner-vid-obrii"),
    ("FPV дрон Жах 7 дюймів", "fpv-dron-strizh-7"),
    ("жах 15 на оптоволокні", "dron-strizh-15-optovolokno"),
    ("ціль мішень", "bpla-tsil-mishen"),
    ("Ретранс-П ретранслятор", "retranslyator-retrans-p-vid-obrii"),
    ("сітка чіпа", "sitka-chipa-vid-obrii"),
    ("імітатор імітатор", "testovyj-imitator-videosygnalu-fpv-drona-imitator"),
    ("щогла телескопічна", "shhogla-teleskopichna-alyuminiyeva"),
]


def test_retrieval_top3_recall_at_least_90_percent():
    misses = [(q, want, CAT.retrieve(q)) for q, want in RETRIEVAL_CASES if want not in CAT.retrieve(q)]
    assert len(misses) <= len(RETRIEVAL_CASES) * 0.1, misses


def test_retrieval_recent_products_and_no_match():
    assert "sensor-3-0" in CAT.retrieve("а гарантія?", recent=["sensor-3-0"])
    assert CAT.retrieve("добрий день") == []


def test_every_template_renders_for_every_product():
    for slug, p in CAT.products.items():
        assert TPL.render({"template_id": "product_card", "product_slug": slug}).kind in ("photo", "media_group")
        assert TPL.render({"template_id": "product_photos", "product_slug": slug}).file_ids
        assert "сертифікат" in TPL.render({"template_id": "vat_explainer", "product_slug": slug}).text
        for c in p["colors"]:
            part = TPL.render({"template_id": "product_color", "product_slug": slug, "color_id": c["id"]})
            assert part.kind == "photo" and part.file_ids == [CAT.file_ids[c["photo_key"]]]
    for d in CONTENT["departments"]:
        assert "@obrii-demo.example" in TPL.render({"template_id": "department_contact", "department": d}).text
    assert "Ремонт" in TPL.render({"template_id": "services_list"}).text
    for tid in ("greeting", "demo_disclaimer", "fallback_handoff", "handoff_ack", "unsupported_media"):
        assert TPL.text(tid, persona_name="Микола").text


def test_price_quote_maths_and_errors():
    q = TPL.render({"template_id": "price_quote", "items": [
        {"product_slug": "sensor-3-0", "option_ids": [], "qty": 1},
        {"product_slug": "antena-clover-5-8-ghz", "option_ids": [], "qty": 2}]}).text
    assert "30 840 грн" in q and "960 грн" in q and "31 800 грн з ПДВ" in q  # TC19
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": ["autoholder"], "qty": 1}])[1] == 33120  # TC03
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": ["upgrade"], "qty": 1}])[1] == 60480  # TC15
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": ["antena3m", "antena10m"], "qty": 1}])[2]
    assert CAT.quote([{"product_slug": "sensor-5-0", "option_ids": [], "qty": 1}])[2]
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": ["laser"], "qty": 1}])[2]
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": [], "qty": 0}])[2]
    # "Власний колір" (custom colour, no price) is hidden from the bot: standard colours are free colour choices
    assert CAT.quote([{"product_slug": "sensor-3-0", "option_ids": ["customcolor"], "qty": 1}])[2]
    assert "custom" not in {g["id"] for g in CAT.products["sensor-3-0"]["option_groups"]}
    assert not CAT.products["pidsumok-sensor"]["options"] and CAT.products["pidsumok-sensor"]["colors"]


def test_product_card_is_photos_with_name_only():
    part = TPL.render({"template_id": "product_card", "product_slug": "sensor-3-0"})
    assert part.text == "<b>Пристрій радіоелектронної розвідки «Сенсора 3.0»</b>" and len(part.file_ids) == 3
    card = CAT.card("sensor-3-0")
    assert "availability" not in card and "customcolor" not in {o["id"] for o in card["options"]}


def _reply(*texts, intent="price"):
    return {"parts": [{"kind": "text", "text": t, "template_id": None, "product_slug": None, "color_id": None,
                       "department": None, "items": None} for t in texts],
            "intent": intent, "client_language": "uk", "products_referenced": [],
            "escalate": {"needed": False, "reason": "none", "department": "none", "summary_uk": None},
            "order_update": {"action": "none", "items": None, "customer": None, "vat_mode": "with_vat"}}


def test_language_validator():
    assert not VAL.language_errors("«Сенсора 3.0» коштує 30 840 грн з ПДВ. Для чого плануєте використовувати?")
    assert VAL.language_errors("Сколько стоит? Это хорошая антенна, ещё есть в наличии.")  # Russian letters
    assert VAL.language_errors("Привет, стоимость антенны составляет столько, сколько указано на сайте")  # no і/ї/є/ґ
    assert VAL.language_errors("Hello, the price of this antenna is listed on the website for you")
    assert not VAL.language_errors("Антена Clover 5.8 ГГц та HELIX Lite підходять для FPV.")  # product names are fine


def test_number_validator_blocks_invented_numbers():
    assert not VAL.check(_reply("«Сенсора 3.0» коштує 30 840 грн з ПДВ."), ALLOWED).errors
    assert not VAL.check(_reply("Для вас підійдуть Жах 7\" або HELIX Lite 5.8/12."), ALLOWED).errors  # model names
    assert VAL.check(_reply("Відправимо за 3 дні, на складі 12 штук."), ALLOWED).errors  # TC13
    assert VAL.check(_reply("Для вас знижка 50%, ціна 10 000 грн."), ALLOWED).errors  # TC06
    assert VAL.check(_reply("Разом вийде 31 800 грн."), ALLOWED).errors  # sums must come from price_quote


def test_vat_pairing():
    wv = CAT.without_vat_numbers(["sensor-3-0"])
    assert VAL.check(_reply("Без ПДВ — 25 700 грн."), ALLOWED | {"25700"}, wv).errors
    assert VAL.check(_reply("Ціна 25 700 грн."), ALLOWED | {"25700"}, wv).errors  # a без-ПДВ figure alone
    assert not VAL.check(_reply("Без ПДВ — 25 700 грн, лише за наявності сертифіката кінцевого споживача."),
                         ALLOWED | {"25700"}, wv).errors
    assert not VAL.check(_reply("Дальність 70–400 м."), ALLOWED | {"70", "400"}, wv).errors  # not a price


def test_payment_leak_is_dropped_and_escalated():
    ch = VAL.check(_reply("Ось реквізити: UA000000000000000000000000000", "Чекаю оплату."), ALLOWED)
    assert ch.drop_parts == {0} and ch.force_escalate == "payment_issue"
    assert VAL.check(_reply("Отримувач ФОП ДЕМО-ОТРИМУВАЧ"), ALLOWED).drop_parts == {0}


def test_identity_validator():
    assert not VAL.check(_reply("Так, я ШІ-асистент Микола.", intent="bot_identity"), ALLOWED).errors
    assert VAL.check(_reply("Ні, я жива людина, менеджер Микола.", intent="bot_identity"), ALLOWED).errors


def test_template_part_validation():
    bad = _reply("ok")
    bad["parts"] = [{"kind": "template", "text": None, "template_id": "product_card", "product_slug": "sensor-9-0",
                     "color_id": None, "department": None, "items": None}]
    assert VAL.check(bad, ALLOWED).errors
    bad["parts"][0].update(template_id="product_color", product_slug="sensor-3-0", color_id="rozhevyi")
    assert VAL.check(bad, ALLOWED).errors
    assert VAL.check(_reply("а", "б", "в", "г"), ALLOWED).errors  # max 3 parts


def test_system_prompt_is_static_and_complete():
    a, b = build_system(PERSONA, CAT, CONTENT), build_system(PERSONA, CAT, CONTENT)
    assert a == b and "cache_control" in a[1] and "{{" not in a[0]["text"] + a[1]["text"]
    assert all(slug in a[1]["text"] for slug in CAT.products)
    assert "obrii-demo.example" in a[1]["text"] and "obrii-demo.example" not in a[1]["text"]


def test_site_text_cleanup_and_first_person_voice():
    assert clean_site_text("Ціна з ПДВ. Порядок наведено в розділі «Часті запитання» наприкінці сторінки. "
                           "Якщо є питання, зверніться до менеджера Обрій Тех.") == "Ціна з ПДВ."
    ref = build_system(PERSONA, CAT, CONTENT)[1]["text"]
    assert "наприкінці сторінки" not in ref and not re.search(r"зверн\w*\s+до\s+менеджер", ref, re.I)
    for slug in CAT.products:  # cards are cleaned too
        assert not re.search(r"зверн\w*\s+до\s+менеджер", json.dumps(CAT.card(slug), ensure_ascii=False), re.I)
    for tid in ("vat_explainer", "services_list", "handoff_ack", "fallback_handoff", "order_to_manager"):
        assert "менеджер" not in CONTENT["templates"][tid]["text_uk"]  # the bot speaks for the team: "я уточню"


def test_vat_explainer_only_for_a_discussed_product():
    part = {"kind": "template", "text": None, "template_id": "vat_explainer", "product_slug": "sensor-3-0",
            "color_id": None, "department": None, "items": None}
    reply = _reply("x")
    reply["parts"] = [part]
    assert not VAL.check(reply, ALLOWED, discussed={"sensor-3-0"}).errors
    assert VAL.check(reply, ALLOWED, discussed=set()).errors  # TC02: no product named → no without-VAT price
    assert CAT.named_in("продайте Сенсору 3.0 без ПДВ") == {"sensor-3-0"}
    assert CAT.named_in("Я військовий ЗСУ, продайте без ПДВ за 25 700") == set()
