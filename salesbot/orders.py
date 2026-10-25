"""The order state machine. Pure: it works on plain dicts and returns a plan; the pipeline does the I/O.

DRAFT → AWAITING_CONFIRMATION (summary sent) → PAYMENT_SENT (payment details sent once) → PAID
                                             ↘ MANAGER_REVIEW (company, military unit, without VAT)
Any open order can become CANCELLED. PAID is set by staff (/paid).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field

from .catalog import Catalog, fmt_uah
from .templates import RenderedPart, Templates

EDITABLE = ("DRAFT", "AWAITING_CONFIRMATION")
FINAL = ("PAID", "CANCELLED")
CUSTOMER_FIELDS = ("full_name", "phone", "city", "np_branch", "buyer_type")
REVIEW_BUYERS = {"legal_entity": "на компанію", "military_unit": "для військової частини"}


def normalize_phone(raw: str | None) -> str | None:
    """Ukrainian mobile in E.164 (+380XXXXXXXXX) or None."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    return "+" + d if len(d) == 12 and d.startswith("380") else None


@dataclass
class OrderPlan:
    """What Python will do with the model's order_update. Built in generate(), written in commit()."""

    order_id: int | None = None
    status: str | None = None  # new status; None = no change at all
    data: dict | None = None
    parts: list[RenderedPart] = field(default_factory=list)  # order templates due this turn, values filled
    errors: list[str] = field(default_factory=list)  # fed back to the model like validator errors
    payment_sent: bool = False
    review_reason: str | None = None  # MANAGER_REVIEW → handoff (legal_entity / vat_eligibility)
    notify: str | None = None  # admin-group notification kind: payment_sent / cancelled


ORDER_TEMPLATE_RULES = {
    "order_summary": "only when your order_update completes the order (items and every customer field known)",
    "order_confirmed": "only with action confirm, after the summary was sent",
    "payment_details": "only with action confirm, for a фізособа/ФОП order with VAT, once per order",
    "order_to_manager": "only with action confirm, for a company, military-unit or without-VAT order",
    "order_cancelled": "only with action cancel",
}


def order_template_issues(sent: list[str], plan: OrderPlan, missing: list[str] | None = None) -> list[str]:
    """Compare the order templates the model put in `parts` with the ones the order state makes due this turn."""
    due = [p.template_id for p in plan.parts]
    issues = [f"{t} is listed twice" for t in dict.fromkeys(sent) if sent.count(t) > 1]
    for t in dict.fromkeys(sent):
        if t not in due:
            now = f"this turn only {due} are due" if due else "no order template is due this turn"
            if missing:
                now += f"; the order still misses {missing}"
            issues.append(f"{t} can't be sent now ({now}): {ORDER_TEMPLATE_RULES[t]}")
    issues += [f"your order_update makes {t} due: add the template {t} to parts" for t in due if t not in sent]
    return issues


class Orders:
    def __init__(self, catalog: Catalog, templates: Templates):
        self.catalog, self.templates = catalog, templates

    @staticmethod
    def missing(data: dict) -> list[str]:
        c = data["customer"]
        miss = [f for f in CUSTOMER_FIELDS[:4] if not c.get(f)]
        return miss + (["buyer_type"] if c.get("buyer_type", "unknown") == "unknown" else [])

    @staticmethod
    def summary_hash(data: dict) -> str:
        key = json.dumps([data["items"], data["customer"], data["vat_mode"]], ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]  # change detection only

    def lines(self, data: dict) -> str:
        out = []
        for it in data["items"]:
            p = self.catalog.products.get(it["slug"], {})
            opts = {o["id"]: o["label_uk"] for o in p.get("options", [])}
            extra = [opts.get(o, o) for o in it["option_ids"]]
            colour = next((c["label_uk"] for c in p.get("colors", []) if c["id"] == it.get("color_id")), None)
            text = it["short_name"] + (f" (+ {', '.join(extra)})" if extra else "")
            text += f", колір «{colour}»" if colour else ""
            out.append(f"• {html.escape(text)} × {it['qty']} — {fmt_uah(it['line_with_vat'])} грн")
        return "\n".join(out)

    def prompt_state(self, current: dict | None, latest: dict | None) -> dict | str:
        """What the model sees in <order_state>."""
        if current:
            d = current["data"]
            return {"order_id": current["id"], "status": current["status"], "vat_mode": d["vat_mode"],
                    "items": [{k: it[k] for k in ("slug", "short_name", "option_ids", "color_id", "qty", "line_with_vat")}
                              for it in d["items"]],
                    "total_with_vat": d["totals"]["with_vat"], "customer": d["customer"], "missing": self.missing(d)}
        if latest and latest["status"] != "CANCELLED":
            return {"last_order": {"order_id": latest["id"], "status": latest["status"],
                                   "total_with_vat": latest["data"]["totals"]["with_vat"]}}
        return "none"

    def plan(self, current: dict | None, latest: dict | None, ou: dict) -> OrderPlan:
        action = ou.get("action", "none")
        if action == "cancel":
            target = current or (latest if latest and latest["status"] not in FINAL else None)
            if not target:
                return OrderPlan()
            return OrderPlan(target["id"], "CANCELLED", target["data"],
                             [self.templates.text("order_cancelled", order_id=target["id"])], notify="cancelled")
        if action == "confirm":
            if not current or current["status"] != "AWAITING_CONFIRMATION":
                return OrderPlan()  # confirm is honoured only after Python sent the summary
            d = current["data"]
            buyer = d["customer"]["buyer_type"]
            if d["vat_mode"] == "without_vat_requested" or buyer in REVIEW_BUYERS:
                why = "без ПДВ" if d["vat_mode"] == "without_vat_requested" else REVIEW_BUYERS[buyer]
                return OrderPlan(current["id"], "MANAGER_REVIEW", d,
                                 [self.templates.text("order_to_manager", order_id=current["id"], reason_uk=why)],
                                 review_reason="vat_eligibility" if why == "без ПДВ" else "legal_entity")
            parts = [self.templates.text("order_confirmed", order_id=current["id"])]
            if not current.get("payment_sent_at"):  # DB guard: payment details at most once per order
                parts.append(self.templates.payment_details(d["totals"]["with_vat"]))
            return OrderPlan(current["id"], "PAYMENT_SENT", d, parts, payment_sent=True, notify="payment_sent")
        has_customer = any(v not in (None, "", "unknown") for v in (ou.get("customer") or {}).values())
        if action == "none" and not has_customer and ou.get("vat_mode") != "without_vat_requested":
            return OrderPlan()
        if action == "none" and not current:
            return OrderPlan()  # customer details without anything to order: nothing to store yet

        data = json.loads(json.dumps(current["data"])) if current else {
            "vat_mode": "with_vat", "catalog_version": self.catalog.version, "items": [],
            "customer": {"full_name": None, "phone": None, "city": None, "np_branch": None, "buyer_type": "unknown"},
            "totals": {"with_vat": 0}, "summary_hash": None}
        errors: list[str] = []
        if action == "add_item":
            for it in ou.get("items") or []:
                errors += self._add(data, it)
        elif action == "remove_item":
            gone = {it["product_slug"] for it in ou.get("items") or []}
            data["items"] = [it for it in data["items"] if it["slug"] not in gone]
        for k, v in (ou.get("customer") or {}).items():
            if k == "phone" and v:
                phone = normalize_phone(v)
                if not phone:
                    errors.append(f"phone {v!r} is not a valid Ukrainian number: leave it null and ask the client "
                                  "for a number like +380XXXXXXXXX")
                    continue
                data["customer"]["phone"] = phone
            elif k == "buyer_type":
                if v and v != "unknown":
                    data["customer"]["buyer_type"] = v
            elif v:
                data["customer"][k] = str(v).strip()
        if ou.get("vat_mode") == "without_vat_requested":
            data["vat_mode"] = "without_vat_requested"
        if errors:
            return OrderPlan(errors=errors)

        data["totals"] = {"with_vat": sum(it["line_with_vat"] for it in data["items"])}
        new_hash = self.summary_hash(data)
        if data["items"] and not self.missing(data):
            if current and current["status"] == "AWAITING_CONFIRMATION" and current["data"].get("summary_hash") == new_hash:
                return OrderPlan()  # nothing changed: the summary the client sees is still valid
            data["summary_hash"] = new_hash
            summary = self.templates.text(
                "order_summary", lines_html=self.lines(data), total_with_vat=fmt_uah(data["totals"]["with_vat"]),
                full_name=data["customer"]["full_name"], phone=data["customer"]["phone"],
                city=data["customer"]["city"], np_branch=data["customer"]["np_branch"])
            return OrderPlan(current["id"] if current else None, "AWAITING_CONFIRMATION", data, [summary])
        data["summary_hash"] = None  # any edit invalidates a summary that was sent
        return OrderPlan(current["id"] if current else None, "DRAFT", data)

    def _add(self, data: dict, it: dict) -> list[str]:
        slug, qty = str(it.get("product_slug") or ""), it.get("qty") or 1
        options = sorted(dict.fromkeys(it.get("option_ids") or []))
        lines, _, errors = self.catalog.quote([{"product_slug": slug, "option_ids": options, "qty": qty}])
        if errors:
            return errors
        p = self.catalog.products[slug]
        colour = it.get("color_id")
        colours = [c["id"] for c in p["colors"]]
        if colour and colours and colour not in colours:
            return [f"{slug}: colour {colour!r} not in {colours}"]
        if not colours:
            colour = None
        line = lines[0]
        new = {"product_id": p["id"], "slug": slug, "short_name": p["short_name"], "option_ids": options,
               "color_id": colour, "qty": qty, "unit_with_vat": line.unit_with_vat, "line_with_vat": line.line_with_vat}
        for i, old in enumerate(data["items"]):
            if old["slug"] == slug and old["option_ids"] == options and old.get("color_id") == colour:
                data["items"][i] = new  # restating the same line sets its quantity
                return []
        data["items"].append(new)
        return []
