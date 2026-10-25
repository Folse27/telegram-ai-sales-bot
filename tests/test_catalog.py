"""Catalog, retrieval, templates, the reply checks and the shop data, on the fictional sample shop."""
from __future__ import annotations

import copy
import json
import re

import pytest
from conftest import ALLOWED, CAT, CONTENT, PERSONA, SHOP, TPL, VAL

from salesbot.catalog import clean_site_text
from salesbot.config import BASE_DIR, SAMPLE_SHOP_DIR
from salesbot.llm import build_system
from salesbot.shop import ShopData, ShopDataError

RETRIEVAL_CASES = [
    ("Скільки коштує Сенсор S3?", "sensor-s3"),
    ("а сенсор s4 маєте?", "sensor-s4"),
    ("sensor s3 price", "sensor-s3"),
    ("цікавить детектор дронів, щоб в авто стояв, скільки з автотримачем?", "sensor-s3"),
    ("автотримач для сенсора окремо продаєте?", "car-holder"),
    ("Скільки коштує Щит 4М?", "shield-4m"),
    ("can we buy Shield 4M for export", "shield-4m"),
    ("глушилка реб на позицію", "shield-4m"),
    ("сколько стоит антенна Омни 5.8", "antenna-omni-5-8"),
    ("всенаправлена антена 5.8", "antenna-omni-5-8"),
    ("спрямована антена спіраль", "antenna-spiral-5-8"),
    ("FPV дрон Стриж 7 дюймів", "strizh-7"),
    ("strizh з акумулятором 6S", "strizh-7"),
    ("зарядна станція енергія", "enerhiia-1200"),
    ("Енергія-1200 скільки коштує", "enerhiia-1200"),
    ("щогла телескопічна", "mast-telescopic"),
    ("підсумок для сенсора", "sensor-pouch"),
    ("чохол на детектор", "sensor-pouch"),
    ("сенсор с3 з відеовиходом", "sensor-s3"),
]


def test_retrieval_top3_recall_at_least_90_percent():
    misses = [(q, want, CAT.retrieve(q)) for q, want in RETRIEVAL_CASES if want not in CAT.retrieve(q)]
    assert len(misses) <= len(RETRIEVAL_CASES) * 0.1, misses


def test_retrieval_recent_products_and_no_match():
    assert "sensor-s3" in CAT.retrieve("а гарантія?", recent=["sensor-s3"])
    assert CAT.retrieve("добрий день") == []


def test_every_template_renders_for_every_product():
    for slug, p in CAT.products.items():
        card = TPL.render({"template_id": "product_card", "product_slug": slug})
        assert card.kind == "photos" and card.photos and card.template_id == "product_card"
        assert TPL.render({"template_id": "product_photos", "product_slug": slug}).photos
        assert "сертифікат" in TPL.render({"template_id": "vat_explainer", "product_slug": slug}).text
        for c in p["colors"]:
            part = TPL.render({"template_id": "product_color", "product_slug": slug, "color_id": c["id"]})
            assert part.kind == "photos" and part.photos == [c["src"]]
    for d in CONTENT["departments"]:
        assert "@obrii-demo.example" in TPL.render({"template_id": "department_contact", "department": d}).text
    assert "Ремонт" in TPL.render({"template_id": "services_list"}).text
    for tid in ("greeting", "demo_disclaimer", "fallback_handoff", "handoff_ack", "limit_reached", "unsupported_media"):
        assert TPL.text(tid, persona_name="Микола").text


def test_price_quote_maths_and_errors():
    q = TPL.render({"template_id": "price_quote", "items": [
        {"product_slug": "sensor-s3", "option_ids": [], "qty": 1},
        {"product_slug": "antenna-omni-5-8", "option_ids": [], "qty": 2}]}).text
    assert "24 000 грн" in q and "1 200 грн" in q and "25 200 грн з ПДВ" in q
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": ["autoholder"], "qty": 1}])[1] == 25800
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": ["upgrade"], "qty": 1}])[1] == 36000  # = S4 price
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": ["antena3m", "antena10m"], "qty": 1}])[2]  # exclusive
    assert CAT.quote([{"product_slug": "sensor-s5", "option_ids": [], "qty": 1}])[2]
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": ["laser"], "qty": 1}])[2]
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": [], "qty": 0}])[2]
    # an on-request option without a price (custom colour) is hidden: standard colours are free choices
    assert CAT.quote([{"product_slug": "sensor-s3", "option_ids": ["customcolor"], "qty": 1}])[2]
    assert "custom" not in {g["id"] for g in CAT.products["sensor-s3"]["option_groups"]}
    assert not CAT.products["sensor-pouch"]["options"] and CAT.products["sensor-pouch"]["colors"]


def test_product_card_is_photos_with_name_only():
    part = TPL.render({"template_id": "product_card", "product_slug": "sensor-s3"})
    assert part.text == "<b>Детектор дронів «Сенсор S3»</b>" and len(part.photos) == 3
    card = CAT.card("sensor-s3")
    assert "availability" not in card and "url" not in card and "customcolor" not in {o["id"] for o in card["options"]}


def _reply(*texts, intent="price"):
    return {"parts": [{"kind": "text", "text": t, "template_id": None, "product_slug": None, "color_id": None,
                       "department": None, "items": None} for t in texts],
            "intent": intent, "client_language": "uk", "products_referenced": [],
            "escalate": {"needed": False, "reason": "none", "department": "none", "summary_uk": None},
            "order_update": {"action": "none", "items": None, "customer": None, "vat_mode": "with_vat"}}


def test_language_validator():
    assert not VAL.language_errors("«Сенсор S3» коштує 24 000 грн з ПДВ. Для чого плануєте використовувати?")
    assert VAL.language_errors("Сколько стоит? Это хорошая антенна, ещё есть в наличии.")  # Russian letters
    assert VAL.language_errors("Привет, стоимость антенны составляет столько, сколько указано на сайте")  # no і/ї/є/ґ
    assert VAL.language_errors("Hello, the price of this antenna is listed on the website for you")
    assert not VAL.language_errors("Антени Омні 5.8 та Спіраль 5.8 підходять для FPV.")  # product names are fine
    assert VAL.language_errors("Дякую! Получатель: Іваненко Петро, відділення 12.")  # Russian word, Ukrainian letters
    assert not VAL.language_errors("Отримувач: Іваненко Петро. Можна оформити замовлення сьогодні.")


def test_number_validator_blocks_invented_numbers():
    assert not VAL.check(_reply("«Сенсор S3» коштує 24 000 грн з ПДВ."), ALLOWED).errors
    assert not VAL.check(_reply("Для вас підійдуть Стриж 7\" або Спіраль 5.8."), ALLOWED).errors  # model names
    assert VAL.check(_reply("Відправимо за 3 дні, на складі 12 штук."), ALLOWED).errors
    assert VAL.check(_reply("Для вас знижка 50%, ціна 10 000 грн."), ALLOWED).errors
    assert VAL.check(_reply("Разом вийде 25 200 грн."), ALLOWED).errors  # sums must come from price_quote


def test_vat_pairing():
    wv = CAT.without_vat_numbers(["sensor-s3"])
    assert VAL.check(_reply("Без ПДВ — 20 000 грн."), ALLOWED | {"20000"}, wv).errors
    assert VAL.check(_reply("Ціна 20 000 грн."), ALLOWED | {"20000"}, wv).errors  # a without-VAT figure alone
    assert not VAL.check(_reply("Без ПДВ — 20 000 грн, лише за наявності сертифіката кінцевого споживача."),
                         ALLOWED | {"20000"}, wv).errors
    assert not VAL.check(_reply("Дальність 70–400 м."), ALLOWED | {"70", "400"}, wv).errors  # not a price


def test_payment_leak_is_dropped_and_escalated():
    ch = VAL.check(_reply("Ось реквізити: UA000000000000000000000000000", "Чекаю оплату."), ALLOWED)
    assert ch.drop_parts == {0} and ch.force_escalate == "payment_issue"
    assert VAL.check(_reply("Отримувач ФОП ДЕМО-ОТРИМУВАЧ"), ALLOWED).drop_parts == {0}
    assert not VAL.check(_reply("Отримувач: Іваненко Петро."), ALLOWED).drop_parts  # the word alone is fine


def test_identity_validator():
    assert not VAL.check(_reply("Так, я ШІ-асистент Микола.", intent="bot_identity"), ALLOWED).errors
    assert VAL.check(_reply("Ні, я жива людина, менеджер Микола.", intent="bot_identity"), ALLOWED).errors


def test_template_part_validation():
    bad = _reply("ok")
    bad["parts"] = [{"kind": "template", "text": None, "template_id": "product_card", "product_slug": "sensor-s9",
                     "color_id": None, "department": None, "items": None}]
    assert VAL.check(bad, ALLOWED).errors
    bad["parts"][0].update(template_id="product_color", product_slug="sensor-s3", color_id="rozhevyi")
    assert VAL.check(bad, ALLOWED).errors
    assert VAL.check(_reply("а", "б", "в", "г"), ALLOWED).errors  # max 3 parts


def test_empty_parts_only_on_order_turns():
    empty = _reply()
    empty["parts"] = []
    assert VAL.check(empty, ALLOWED).errors
    empty["order_update"]["action"] = "confirm"
    assert not VAL.check(empty, ALLOWED).errors


def test_system_prompt_is_static_and_complete():
    a = build_system(PERSONA, CAT, CONTENT, SHOP.examples)
    assert a == build_system(PERSONA, CAT, CONTENT, SHOP.examples)
    assert "cache_control" in a[1] and "{{" not in a[0]["text"] + a[1]["text"]
    assert all(slug in a[1]["text"] for slug in CAT.products)
    assert "obrii-demo.example" in a[1]["text"] and "Обрій Тех" in a[0]["text"]
    assert not any(p["url"] in a[0]["text"] + a[1]["text"] for p in CAT.products.values())  # no page links


def test_site_text_cleanup_and_first_person_voice():
    assert clean_site_text("Ціна з ПДВ. Порядок наведено в розділі «Часті запитання» наприкінці сторінки. "
                           "Якщо є питання, зверніться до менеджера «Обрій Тех».") == "Ціна з ПДВ."
    ref = build_system(PERSONA, CAT, CONTENT, SHOP.examples)[1]["text"]
    assert "наприкінці сторінки" not in ref and not re.search(r"зверн\w*\s+до\s+менеджер", ref, re.I)
    for slug in CAT.products:  # cards are cleaned too
        assert not re.search(r"зверн\w*\s+до\s+менеджер", json.dumps(CAT.card(slug), ensure_ascii=False), re.I)
    for tid in ("vat_explainer", "services_list", "handoff_ack", "fallback_handoff", "order_to_manager", "payment_received"):
        assert "менеджер" not in CONTENT["templates"][tid]["text_uk"]  # the bot speaks for the team: "я уточню"


def test_vat_explainer_only_for_a_discussed_product():
    part = {"kind": "template", "text": None, "template_id": "vat_explainer", "product_slug": "sensor-s3",
            "color_id": None, "department": None, "items": None}
    reply = _reply("x")
    reply["parts"] = [part]
    assert not VAL.check(reply, ALLOWED, discussed={"sensor-s3"}).errors
    assert VAL.check(reply, ALLOWED, discussed=set()).errors  # no product named → no without-VAT price
    assert CAT.named_in("продайте Сенсор S3 без ПДВ") == {"sensor-s3"}
    assert CAT.named_in("Я військовий ЗСУ, продайте без ПДВ за 20 000") == set()


def test_sample_shop_is_valid_and_problems_are_reported():
    assert SHOP.problems() == []
    broken = copy.deepcopy(SHOP)
    del broken.content["templates"]["limit_reached"]
    broken.catalog["products"][0]["price"]["with_vat"] += 1
    problems = broken.problems()
    assert any("limit_reached" in p for p in problems) and any("sensor-s3 price" in p for p in problems)
    texts = ShopData.read_folder(SAMPLE_SHOP_DIR)
    with pytest.raises(ShopDataError, match=r"examples\.md"):
        ShopData.parse({k: v for k, v in texts.items() if k != "examples.md"}, "test")
    with pytest.raises(ShopDataError):
        ShopData.parse({**texts, "catalog.json": "{not json"}, "test")


def test_all_photo_sources_exist_in_the_repository():
    assert all((BASE_DIR / src).is_file() for src in CAT.all_photo_sources())
