"""Post-generation checks on the model's reply. Text parts are checked; template output is Python's and trusted."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import Catalog
from .templates import MODEL_TEMPLATES, ORDER_TEMPLATES

PRICE_RE = re.compile(r"(\d{1,3}(?:[   ]\d{3})+|\d+)\s*грн", re.I)  # a number written as a price
NUM_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+(?:[.,]\d+)?")
URL_EMAIL_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+")
RUSSIAN_LETTERS_RE = re.compile(r"[ыэъёЫЭЪЁ]")
UKRAINIAN_LETTERS_RE = re.compile(r"[іїєґІЇЄҐ]")
RUSSIAN_WORDS_RE = re.compile(  # Russian-only words without ы/э/ъ/ё (smaller models slip them into Ukrainian)
    r"\b(получател\w*|сколько|стоит|стоимост\w*|наличи[еи]|спасибо|пожалуйста|здравствуйте|можно|нужно|конечно|"
    r"сейчас|только|когда|хорошо|если|пока что)\b", re.I)
SUMMARY_TALK_RE = re.compile(r"перевір|отримувач|получат|разом", re.I)
PROMISE_RE = re.compile(r"уточн\w*|підключаю колегу|напише вам тут|напишу вам тут|прийнято", re.I)
COURTESY_ONLY_RE = re.compile(r"(дякую|дякуємо|чудово|добре|відмінно|супер|звісно)[\s!.,]*(\S{0,12}[!.]*)?", re.I)
IDENTITY_OK_RE = re.compile(r"бот|штучн|ШІ", re.I)
HUMAN_CLAIM_RE = re.compile(r"\bя\s+(жива\s+)?людина\b", re.I)
IBAN_RE = re.compile(r"UA\d{27}")
CARD_RE = re.compile(r"\b(?:\d[ -]?){16}\b")


def numbers_in(text: str) -> set[str]:
    return {re.sub(r"[   ]", "", m).replace(",", ".") for m in NUM_RE.findall(URL_EMAIL_RE.sub(" ", text))}


def reference_numbers(system: list[dict]) -> set[str]:
    """Numbers from the cached prompt that may appear in replies: prices and decimals, not small integers.
    Small integers there are model names (Стриж 7", кабель 3 м) and would whitelist invented days or quantities."""
    return {n for n in numbers_in(system[0]["text"] + system[1]["text"]) if "." in n or len(n) >= 3}


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)
    drop_parts: set[int] = field(default_factory=set)  # payment-leak parts: removed, not retried
    force_escalate: str | None = None


class Validator:
    def __init__(self, catalog: Catalog, content: dict, max_parts: int = 3):
        self.max_parts = max_parts
        self.catalog = catalog
        pay = content["payment"]
        markers = [pay.get("iban"), pay.get("payee_code"), *pay.get("markers", [pay.get("payee"), pay.get("bank")])]
        self.payment_markers = [str(m).lower() for m in markers if m]
        self.departments = set(content["departments"])

    def language_errors(self, text: str) -> list[str]:
        errs = []
        if RUSSIAN_LETTERS_RE.search(text):
            errs.append("uses Russian letters (ы/э/ъ/ё): write in Ukrainian only")
        russian = sorted({m.lower() for m in RUSSIAN_WORDS_RE.findall(text)})
        if russian:
            errs.append(f"Russian words {russian}: use the Ukrainian ones (отримувач, скільки, коштує, можна, дякую…)")
        words = re.findall(r"[^\W\d_]+", URL_EMAIL_RE.sub(" ", text))
        cyr = sum(len(w) for w in words if re.match(r"[а-яіїєґ']", w, re.I))
        lat = sum(len(w) for w in words if re.match(r"[a-z]", w, re.I) and w.lower() not in self.catalog.latin_words)
        if cyr + lat and cyr / (cyr + lat) < 0.8:
            errs.append("text is not in Ukrainian")
        if len(text) > 40 and not UKRAINIAN_LETTERS_RE.search(text):
            errs.append("text looks Russian (no і/ї/є/ґ): write natural Ukrainian")
        return errs

    def check(self, data: dict, allowed_numbers: set[str], without_vat: set[str] | frozenset = frozenset(),
              discussed: set[str] | None = None) -> Check:
        """`discussed`: products the client named now or the last reply was about. The vat_explainer quotes a
        price, so it is only accepted for one of them (None = no restriction, used by unit tests of other checks)."""
        ch, cat = Check(), self.catalog
        parts = data.get("parts") or []
        ordering = (data.get("order_update") or {}).get("action", "none") != "none"
        counted = [p for p in parts if p.get("template_id") not in ORDER_TEMPLATES]  # order templates don't count
        if len(counted) > self.max_parts or (not parts and not ordering):  # 0 parts only when Python sends the order
            ch.errors.append(f"parts must contain 1 to {self.max_parts} items (order templates not counted), "
                             f"got {len(counted)}")
        for i, part in enumerate(parts):
            where = f"part {i + 1}"
            if part.get("kind") == "text":
                text = (part.get("text") or "").strip()
                if not text:
                    ch.errors.append(f"{where}: kind=text needs non-empty text")
                    continue
                if len(text) > 700:
                    ch.errors.append(f"{where}: text is {len(text)} characters, max 700")
                if self._payment_leak(text):
                    ch.drop_parts.add(i)
                    ch.force_escalate = "payment_issue"
                    continue
                ch.errors += [f"{where}: {e}" for e in self.language_errors(text)]
                stray = sorted(numbers_in(cat.strip_names(text)) - allowed_numbers)
                if stray:
                    ch.errors.append(f"{where}: numbers {stray} are not in the catalog cards or the client's messages. "
                                     "Do not invent dates, quantities, discounts or totals; use price_quote for sums.")
                prices = {re.sub(r"\D", "", m) for m in PRICE_RE.findall(text)}
                if (re.search(r"без\s+ПДВ", text, re.I) or prices & without_vat) and "сертифікат" not in text.lower():
                    ch.errors.append(f"{where}: a price without VAT must come with the сертифікат кінцевого споживача "
                                     "condition (or use the vat_explainer template)")
                if data.get("intent") == "bot_identity" and (not IDENTITY_OK_RE.search(text) or HUMAN_CLAIM_RE.search(text)):
                    ch.errors.append(f"{where}: say truthfully that you are an AI assistant (ШІ-асистент), never a human")
            elif part.get("kind") == "template":
                ch.errors += [f"{where}: {e}" for e in self._template_errors(part)]
                if part.get("template_id") == "vat_explainer" and discussed is not None \
                        and part.get("product_slug") not in discussed:
                    ch.errors.append(f"{where}: vat_explainer quotes a price, so use it only for a product the client "
                                     "is asking about. No product is clear here: explain the сертифікат кінцевого "
                                     "споживача condition in text without any price and ask which product they need.")
            else:
                ch.errors.append(f"{where}: unknown kind")
        return ch

    def _template_errors(self, part: dict) -> list[str]:
        tid, cat = part.get("template_id"), self.catalog
        if tid in ORDER_TEMPLATES:
            return []  # checked against the order state in the pipeline (order_template_issues)
        if tid not in MODEL_TEMPLATES:
            return [f"template_id {tid!r} is not allowed"]
        if tid in ("product_card", "product_photos", "product_color", "vat_explainer"):
            slug = part.get("product_slug")
            if slug not in cat.products:
                return [f"{tid}: unknown product_slug {slug!r} (use a slug from the catalog index)"]
            colours = [c["id"] for c in cat.products[slug]["colors"]]
            if tid == "product_color" and part.get("color_id") not in colours:
                return [f"product_color: colour {part.get('color_id')!r} not in {colours}"]
        if tid == "department_contact" and part.get("department") not in self.departments:
            return [f"department_contact: unknown department {part.get('department')!r}"]
        if tid == "price_quote":
            return cat.quote(part.get("items") or [])[2]
        return []

    def _payment_leak(self, text: str) -> bool:
        low = text.lower()
        return bool(IBAN_RE.search(text) or CARD_RE.search(text)
                    or any(m in low for m in self.payment_markers)
                    or re.search(r"реквізит\w*[^\n]{0,40}\d{6,}", text, re.I))
