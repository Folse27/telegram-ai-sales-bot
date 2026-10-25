"""Order state machine and the reply assembly around it (pure, no database)."""
from __future__ import annotations

from conftest import ALLOWED, CAT, ORD, TPL, VAL
from test_catalog import _reply

from salesbot.orders import OrderPlan, normalize_phone, order_template_issues
from salesbot.pipeline import LlmPipeline
from salesbot.validation import Check

CUSTOMER = {"full_name": "Іваненко Петро", "phone": "067 123 45 67", "city": "Київ", "np_branch": "12",
            "buyer_type": "individual"}
SENSOR = {"product_slug": "sensor-s3", "option_ids": [], "color_id": None, "qty": 1}
IBAN = "UA000000000000000000000000000"


def _ou(action="none", items=None, customer=None, vat_mode="with_vat"):
    return {"action": action, "items": items, "customer": customer, "vat_mode": vat_mode}


def _row(plan, oid=17, payment_sent_at=None):
    return {"id": oid, "status": plan.status, "data": plan.data, "payment_sent_at": payment_sent_at}


def test_phone_normalisation():
    assert normalize_phone("067 123 45 67") == "+380671234567"
    assert normalize_phone("+38 (067) 123-45-67") == "+380671234567"
    assert normalize_phone("380671234567") == "+380671234567"
    assert normalize_phone("12345") is None and normalize_phone(None) is None


def test_individual_order_to_payment_details():
    p1 = ORD.plan(None, None, _ou("add_item", [{**SENSOR, "option_ids": ["videovykhid"], "color_id": "oliva"}]))
    assert p1.status == "DRAFT" and p1.order_id is None and not p1.parts
    assert p1.data["totals"]["with_vat"] == 24480 and p1.data["items"][0]["product_id"] == 101
    p2 = ORD.plan(_row(p1), _row(p1), _ou("set_customer", customer=CUSTOMER))
    assert p2.status == "AWAITING_CONFIRMATION" and p2.data["customer"]["phone"] == "+380671234567"
    summary = p2.parts[0].text
    assert "24 480 грн з ПДВ" in summary and "колір «олива»" in summary and "Відеовихід" in summary
    assert "Іваненко Петро, +380671234567" in summary and "відділення 12" in summary
    p3 = ORD.plan(_row(p2), _row(p2), _ou("confirm"))
    assert p3.status == "PAYMENT_SENT" and p3.payment_sent and p3.notify == "payment_sent"
    assert "Замовлення №17 підтверджено" in p3.parts[0].text
    assert "Оплата : 24 480 грн" in p3.parts[1].text and IBAN in p3.parts[1].text
    again = ORD.plan(_row(p2, payment_sent_at="2026-10-01"), None, _ou("confirm"))
    assert len(again.parts) == 1  # payment details at most once per order


def test_confirm_needs_a_summary_first():
    p1 = ORD.plan(None, None, _ou("add_item", [SENSOR]))
    assert ORD.plan(_row(p1), _row(p1), _ou("confirm")).status is None  # DRAFT: confirm ignored


def test_company_order_goes_to_review_without_payment():
    p1 = ORD.plan(None, None, _ou("add_item", [{**SENSOR, "product_slug": "sensor-s4", "qty": 3}],
                                  customer={**CUSTOMER, "buyer_type": "legal_entity"}))
    assert p1.status == "AWAITING_CONFIRMATION" and "108 000 грн з ПДВ" in p1.parts[0].text
    p2 = ORD.plan(_row(p1), _row(p1), _ou("confirm"))
    assert p2.status == "MANAGER_REVIEW" and p2.review_reason == "legal_entity" and not p2.payment_sent
    assert "на компанію" in p2.parts[0].text and not any(IBAN in x.text for x in p2.parts)


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


# ── the model chooses the order templates; Python checks they're due and fills the values ──

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


def _build(parts, plan):
    pipeline = LlmPipeline(None, None, None, CAT, TPL, VAL, None, ORD, None, None)  # type: ignore[arg-type]
    return pipeline._build({**_reply(), "parts": parts}, Check(), plan).parts


def test_build_uses_the_model_order_and_python_values():
    summary = _build([_reply("Дякую!")["parts"][0], _tpl_part("order_summary")], _summary_plan())
    assert [p.template_id for p in summary] == ["order_summary"]  # the bare «Дякую!» is dropped
    assert summary[0].text.startswith("Перевірте, будь ласка, замовлення")
    keep = _build([_tpl_part("order_summary"), _reply("Гарантія — 12 місяців.")["parts"][0]], _summary_plan())
    assert [p.template_id for p in keep] == ["order_summary", None]  # a real answer stays, in the model's order
    confirm = _build([_reply("Реквізити надійдуть автоматично")["parts"][0], _tpl_part("order_confirmed"),
                      _tpl_part("payment_details")], _confirm_plan())
    assert [p.template_id for p in confirm] == ["order_confirmed", "payment_details"]  # free text dropped
    assert "Оплата : 24 000 грн" in confirm[1].text


def test_build_corrects_order_templates_on_the_second_attempt():
    fixed = _build([_tpl_part("payment_details")], _summary_plan())  # not due → dropped; due summary → appended
    assert [p.template_id for p in fixed] == ["order_summary"]
    company = _build([_tpl_part("order_confirmed"), _tpl_part("payment_details")], _confirm_plan("legal_entity"))
    assert [p.template_id for p in company] == ["order_to_manager"]  # never payment details for a company
    empty = _build([], _summary_plan())  # regression: no fallback text in front of the summary
    assert [p.template_id for p in empty] == ["order_summary"]
