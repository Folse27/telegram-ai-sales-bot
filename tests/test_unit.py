"""Unit tests. Debounce tests run on a virtual clock (no network). Storage tests need TEST_DATABASE_URL."""
from __future__ import annotations

import json
import random
import re
import uuid
from types import SimpleNamespace

import pytest
from conftest import FakeClock, FakePipeline, FakeTransport

import main
from config import Settings
from main import (IDLE, LLM, SENDING, Catalog, Check, Handoff, Incoming, LlmPipeline, OrderPlan, Orders, SessionConfig,
                  SessionManager, Storage, Templates, Validator, build_system, clean_site_text, normalize_phone,
                  order_template_issues, reference_numbers)

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
    assert VAL.language_errors("Дякую! Получатель: Іваненко Петро, відділення 12.")  # Russian word, Ukrainian letters
    assert not VAL.language_errors("Отримувач: Іваненко Петро. Можна оформити замовлення сьогодні.")


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
    for tid in ("vat_explainer", "services_list", "handoff_ack", "fallback_handoff", "order_to_manager", "payment_received"):
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


# ═══════════════════════════ orders (Step 6, pure) ═══════════════════════════

ORD = Orders(CAT, TPL)
CUSTOMER = {"full_name": "Іваненко Петро", "phone": "067 123 45 67", "city": "Київ", "np_branch": "12",
            "buyer_type": "individual"}
SENSOR = {"product_slug": "sensor-3-0", "option_ids": [], "color_id": None, "qty": 1}


def _ou(action="none", items=None, customer=None, vat_mode="with_vat"):
    return {"action": action, "items": items, "customer": customer, "vat_mode": vat_mode}


def _row(plan, oid=17, payment_sent_at=None):
    return {"id": oid, "status": plan.status, "data": plan.data, "payment_sent_at": payment_sent_at}


def test_phone_normalisation():
    assert normalize_phone("067 123 45 67") == "+380671234567"
    assert normalize_phone("+38 (067) 123-45-67") == "+380671234567"
    assert normalize_phone("380671234567") == "+380671234567"
    assert normalize_phone("12345") is None and normalize_phone(None) is None


def test_tc09_individual_order_to_payment_details():
    p1 = ORD.plan(None, None, _ou("add_item", [{**SENSOR, "option_ids": ["videovykhid"], "color_id": "oliva"}]))
    assert p1.status == "DRAFT" and p1.order_id is None and not p1.parts
    assert p1.data["totals"]["with_vat"] == 31200 and p1.data["items"][0]["product_id"] == 9333
    p2 = ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer=CUSTOMER))
    assert p2.status == "AWAITING_CONFIRMATION" and p2.data["customer"]["phone"] == "+380671234567"
    summary = p2.parts[0].text
    assert "31 200 грн з ПДВ" in summary and "колір «олива»" in summary and "Відеовихід" in summary
    assert "Іваненко Петро, +380671234567" in summary and "відділення 12" in summary
    p3 = ORD.plan(_row(p2), _row(p2), _ou("confirm"))
    assert p3.status == "PAYMENT_SENT" and p3.payment_sent and p3.notify == "payment_sent"
    assert "Замовлення №17 підтверджено" in p3.parts[0].text
    assert "Оплата : 31 200 грн" in p3.parts[1].text and "UA000000000000000000000000000" in p3.parts[1].text
    again = ORD.plan(_row(p2, payment_sent_at="2026-10-01"), None, _ou("confirm"))
    assert len(again.parts) == 1  # payment details at most once per order


def test_confirm_needs_a_summary_first():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR]))
    assert ORD.plan(_row(p1), _row(p1), _ou("confirm")).status is None  # DRAFT: confirm ignored


def test_tc10_company_order_goes_to_review_without_payment():
    p1 = ORD.plan(None, None, _ou("add_item", [{**SENSOR, "product_slug": "sensor-4-0", "qty": 3}],
                                  customer={**CUSTOMER, "buyer_type": "legal_entity"}))
    assert p1.status == "AWAITING_CONFIRMATION" and "181 440 грн з ПДВ" in p1.parts[0].text
    p2 = ORD.plan(_row(p1), _row(p1), _ou("confirm"))
    assert p2.status == "MANAGER_REVIEW" and p2.review_reason == "legal_entity" and not p2.payment_sent
    assert "на компанію" in p2.parts[0].text and not any("UA14" in x.text for x in p2.parts)


def test_without_vat_request_goes_to_review():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR], customer=CUSTOMER, vat_mode="without_vat_requested"))
    p2 = ORD.plan(_row(p1), _row(p1), _ou("confirm"))
    assert p2.status == "MANAGER_REVIEW" and p2.review_reason == "vat_eligibility"


def test_edit_after_summary_sends_a_new_summary():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR], customer=CUSTOMER))
    assert ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer=CUSTOMER)).status is None  # unchanged: no repeat
    p2 = ORD.plan(_row(p1), _row(p1), _ou("add_item", [{**SENSOR, "qty": 2}]))
    assert p2.status == "AWAITING_CONFIRMATION" and p2.data["summary_hash"] != p1.data["summary_hash"]
    assert p2.data["items"][0]["qty"] == 2 and len(p2.data["items"]) == 1  # restating a line sets its quantity
    p3 = ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer={"full_name": None, "phone": None, "city": "Львів",
                                                                     "np_branch": None, "buyer_type": "unknown"}))
    assert "Львів" in p3.parts[0].text


def test_order_validation_errors_are_reported():
    assert ORD.plan(None, None, _ou("add_item", [{**SENSOR, "color_id": "rozhevyi"}])).errors
    assert ORD.plan(None, None, _ou("add_item", [{**SENSOR, "option_ids": ["laser"]}])).errors
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR]))
    assert ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer={**CUSTOMER, "phone": "12345"})).errors


def test_cancel():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR]))
    c = ORD.plan(_row(p1), _row(p1), _ou("cancel"))
    assert c.status == "CANCELLED" and "№17 скасовано" in c.parts[0].text and c.notify == "cancelled"
    assert ORD.plan(None, {"id": 3, "status": "PAID", "data": p1.data}, _ou("cancel")).status is None


# ═══════════════════════════ handoff (Step 6, test DB) ═══════════════════════════

ADMIN = -100500


@pytest.mark.db
async def test_handoff_card_relay_buttons_and_expiry(storage):
    tr = FakeTransport()
    ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN), storage, tr, TPL, ORD)
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


@pytest.mark.db
async def test_admin_commands(storage):
    tr = FakeTransport()
    ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN), storage, tr, TPL, ORD)
    await storage.upsert_client(43, 43, None, "Олена")
    oid = await storage.pool.fetchval(
        "INSERT INTO orders (chat_id, status, data) VALUES (43, 'PAYMENT_SENT', $1) RETURNING id",
        {"items": [], "totals": {"with_vat": 31200}, "customer": {}, "vat_mode": "with_vat"})
    assert "№" in await ho.command("/orders")
    assert "✅" in await ho.command(f"/paid@SampleShopBot {oid}")
    assert await storage.pool.fetchval("SELECT status FROM orders WHERE id = $1", oid) == "PAID"
    assert tr.of("text")[-1][1] == 43 and "Оплату отримано" in tr.of("text")[-1][2]
    assert "уже PAID" in await ho.command(f"/cancel {oid}")
    assert "Формат" in await ho.command("/paid abc")


# ═══════════════════════════ owner feedback round 2: models, empty parts, resume, /clear ═══════════════════════════

def _llm(model, thinking="adaptive"):
    s = Settings(_env_file=None, telegram_bot_token="1:x", webhook_secret="x", anthropic_api_key="test",
                 model=model, llm_thinking=thinking)
    return LLM(s, CAT, CONTENT, None)


def test_request_shape_per_model():
    haiku = _llm("claude-haiku-4-5").request([])
    assert haiku["tool_choice"] == {"type": "tool", "name": "reply"} and haiku["extra_body"] == {"temperature": 0.2}
    assert "thinking" not in haiku and "output_config" not in haiku and haiku["max_tokens"] == 1200
    s55 = _llm("claude-sonnet-5-5").request([])  # docs: no forced tool use, no "disabled", no temperature
    assert s55["tool_choice"] == {"type": "auto"} and s55["thinking"] == {"type": "adaptive"}
    assert s55["output_config"] == {"effort": "low"} and s55["max_tokens"] == 4000 and "extra_body" not in s55
    assert _llm("claude-sonnet-5-5", "between_tools").request([])["thinking"] == {"type": "between_tools"}
    s5 = _llm("claude-sonnet-5").request([])
    assert s5["tool_choice"]["type"] == "tool" and s5["thinking"] == {"type": "disabled"}
    o55 = _llm("claude-opus-5-5").request([])
    assert o55["tool_choice"] == {"type": "auto"} and "thinking" not in o55 and o55["output_config"] == {"effort": "low"}


async def test_rejected_params_fall_back_to_safe_ones():
    class FakeBadRequest(Exception):
        pass

    llm = _llm("claude-sonnet-5-5")
    llm.anthropic = SimpleNamespace(BadRequestError=FakeBadRequest)
    seen = []

    async def create(**req):
        seen.append(req)
        if len(seen) == 1:
            raise FakeBadRequest("thinking: unsupported value")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="reply", id="t1", input={"parts": []})],
                               stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                               cache_read_input_tokens=0, cache_creation_input_tokens=0))

    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    _, call, _ = await llm.call(1, [], 1)
    assert call["id"] == "t1" and "thinking" not in seen[1] and seen[1]["tool_choice"] == {"type": "auto"}


def test_empty_parts_only_on_order_turns():
    empty = _reply()
    empty["parts"] = []
    assert VAL.check(empty, ALLOWED).errors
    empty["order_update"]["action"] = "confirm"
    assert not VAL.check(empty, ALLOWED).errors


async def test_session_drop_cancels_its_tasks():
    clock, _, pl, sm = setup()
    await say(sm, "q", 1)
    sm.drop(CHAT)
    await clock.advance(30)
    assert pl.calls == [] and CHAT not in sm.sessions


@pytest.mark.db
async def test_resume_mapping_and_wipe(storage):
    tr = FakeTransport()
    ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN), storage, tr, TPL, ORD)
    resumed = []

    async def on_resume(chat_id):
        resumed.append((chat_id, [r["text"] for r in await ho.trailing_client_messages(chat_id)]))

    ho.on_resume = on_resume
    await storage.upsert_client(44, 44, None, "Роман")
    await storage.save_message(44, "user", "покличте менеджера", 1, answered=False)
    await ho.open(44, "client_request", "sales", "Клієнт просить менеджера", human=True)
    hid = await storage.pool.fetchval("SELECT id FROM handoffs WHERE chat_id = 44")
    await ho.button(hid, "take", "Роман")
    take_notice = await storage.pool.fetchval(
        "SELECT max(admin_message_id) FROM relay_map WHERE chat_id = 44")  # the "✋ взяв" notice is mapped too
    assert await ho.manager_reply(take_notice, 777, "Вітаю, з вами Сергій") == 44
    await storage.save_message(44, "user", "Можете підсказати, як проходить оплата на ФОП?", 2, answered=True)
    await ho.button(hid, "bot", "Роман")
    assert resumed == [(44, ["Можете підсказати, як проходить оплата на ФОП?"])]  # answered after the hand-back

    await ho.notify_order("payment_sent", 1, 44)  # replies to order notifications reach the client as well
    note = tr.of("text")[-1]
    assert await storage.pool.fetchval("SELECT chat_id FROM relay_map WHERE admin_message_id = $1",
                                       await storage.pool.fetchval("SELECT max(admin_message_id) FROM relay_map")) == 44
    assert note[1] == ADMIN

    await storage.wipe_chat(44)
    for table in ("messages", "orders", "handoffs", "relay_map"):
        assert await storage.pool.fetchval(f"SELECT count(*) FROM {table} WHERE chat_id = 44") == 0
    assert await storage.get_mode(44) == "BOT"


# ═══════════════════════════ round 3: model-chosen order templates, CACHE_TTL ═══════════════════════════

def _tpl_part(tid):
    return {"kind": "template", "text": None, "template_id": tid, "product_slug": None, "color_id": None,
            "department": None, "items": None}


def _summary_plan():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR]))
    return ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer=CUSTOMER))


def _confirm_plan(buyer="individual"):
    p = ORD.plan(None, None, _ou("add_item", [SENSOR], customer={**CUSTOMER, "buyer_type": buyer}))
    return ORD.plan(_row(p), _row(p), _ou("confirm"))


def test_order_templates_due_per_turn():
    assert [p.template_id for p in _summary_plan().parts] == ["order_summary"]
    assert [p.template_id for p in _confirm_plan().parts] == ["order_confirmed", "payment_details"]
    assert [p.template_id for p in _confirm_plan("legal_entity").parts] == ["order_to_manager"]
    assert not order_template_issues(["order_summary"], _summary_plan())
    assert order_template_issues([], _summary_plan())  # due but missing
    assert order_template_issues(["order_summary", "payment_details"], _summary_plan())  # not due yet
    assert order_template_issues(["order_confirmed", "payment_details"], _confirm_plan("legal_entity"))  # company
    assert order_template_issues(["order_summary", "order_summary"], _summary_plan())  # twice
    assert "still misses" in order_template_issues(["order_summary"], OrderPlan(), ["phone"])[0]
    assert not VAL.check({**_reply(), "parts": [_tpl_part("payment_details")]}, ALLOWED).errors  # checked later


def _pipeline():
    return LlmPipeline(None, None, None, CAT, TPL, VAL, None, ORD, None)


def _build(parts, plan):
    return _pipeline()._build({**_reply(), "parts": parts}, Check(), plan).parts


def test_build_uses_the_model_order_and_python_values():
    summary = _build([_reply("Дякую!")["parts"][0], _tpl_part("order_summary")], _summary_plan())
    assert [p.template_id for p in summary] == ["order_summary"]  # the bare «Дякую!» is dropped
    assert summary[0].text.startswith("Перевірте, будь ласка, замовлення")
    keep = _build([_tpl_part("order_summary"), _reply("Гарантія — 12 місяців.")["parts"][0]], _summary_plan())
    assert [p.template_id for p in keep] == ["order_summary", None]  # a real answer stays, in the model's order
    confirm = _build([_reply("Реквізити надійдуть автоматично")["parts"][0], _tpl_part("order_confirmed"),
                      _tpl_part("payment_details")], _confirm_plan())
    assert [p.template_id for p in confirm] == ["order_confirmed", "payment_details"]  # free text dropped
    assert "Оплата : 30 840 грн" in confirm[1].text


def test_build_corrects_order_templates_on_the_second_attempt():
    fixed = _build([_tpl_part("payment_details")], _summary_plan())  # not due → dropped; due summary → appended
    assert [p.template_id for p in fixed] == ["order_summary"]
    company = _build([_tpl_part("order_confirmed"), _tpl_part("payment_details")], _confirm_plan("legal_entity"))
    assert [p.template_id for p in company] == ["order_to_manager"]  # never payment details for a company
    empty = _build([], _summary_plan())  # regression: no fallback text in front of the summary
    assert [p.template_id for p in empty] == ["order_summary"]


def test_cache_ttl_setting():
    one_hour = build_system(SimpleNamespace(persona_name="Микола", cache_ttl="1h"), CAT, CONTENT)[1]["cache_control"]
    assert one_hour == {"type": "ephemeral", "ttl": "1h"}
    assert build_system(SimpleNamespace(persona_name="Микола", cache_ttl="5m"), CAT, CONTENT)[1]["cache_control"] == {
        "type": "ephemeral"}
    with pytest.raises(Exception):
        Settings(_env_file=None, telegram_bot_token="1:x", webhook_secret="x", cache_ttl="10m")


@pytest.mark.db
async def test_handoff_ack_only_when_nothing_promised_a_follow_up(storage):
    tr = FakeTransport()
    ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN), storage, tr, TPL, ORD)
    pl = LlmPipeline(None, storage, tr, CAT, TPL, VAL, None, ORD, ho)
    esc = {"needed": True, "reason": "vat_eligibility", "department": "sales", "summary_uk": "без ПДВ"}
    for chat in (45, 46):
        await storage.upsert_client(chat, chat, None, "Тест")
    vat = TPL.render({"template_id": "vat_explainer", "product_slug": "sensor-3-0"})  # ends with «я уточню…»
    await pl.commit(45, [], [vat], main.Reply([vat], {"escalate": esc}))
    assert not [c for c in tr.of("text") if c[1] == 45]  # no second promise to the client
    card = TPL.render({"template_id": "product_card", "product_slug": "sensor-3-0"})  # no promise in it
    await pl.commit(46, [], [card], main.Reply([card], {"escalate": esc}))
    assert [c[2] for c in tr.of("text") if c[1] == 46] == [TPL.text("handoff_ack").text]


# ═══════════════════════════ Step 8: /stats and latency ═══════════════════════════

async def test_first_part_latency_is_measured_from_the_last_client_message():
    clock, _, pl, sm = setup()
    await say(sm, "привіт", 1)
    await clock.advance(3)
    await say(sm, "є чохли?", 2)  # last message at t=3 → call at 13, first part at 15
    await clock.advance(20)
    assert pl.extras[-1]["first_part_after_s"] == 12.0


def test_llm_call_cost():
    row = {"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 500,
           "cache_read_tokens": 15000, "cache_write_tokens": 0}
    assert main.llm_call_cost(row) == pytest.approx((1000 * 2 + 500 * 10 + 15000 * 0.2) / 1e6)
    row = {**row, "model": "claude-haiku-4-5-20251001", "cache_read_tokens": 0, "cache_write_tokens": 10000}
    assert main.llm_call_cost(row, "5m") == pytest.approx((1000 + 2500 + 10000 * 1.25) / 1e6)


@pytest.mark.db
async def test_stats_command(storage):
    ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN, cache_ttl="1h"), storage, FakeTransport(), TPL, ORD)
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
