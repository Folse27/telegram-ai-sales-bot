"""The product catalog: retrieval for the prompt, compact product cards and all price maths."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

TOKEN_RE = re.compile(r"[\w'.]+")
APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'"})
QUOTES_RE = re.compile(r"[\"«»“”„″]")
# Sentences from scraped product pages that only make sense on the website ("see the FAQ at the bottom of the
# page", "contact the manager"): dropped so the bot never sends the client somewhere else.
SITE_ONLY_RE = re.compile(r"наприкінці сторінки|«Часті запитання»|зверн\w*\s+до\s+менеджер", re.I)


def fmt_uah(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def clean_site_text(text: str) -> str:
    return " ".join(s for s in re.split(r"(?<=[.!?])\s+", text) if not SITE_ONLY_RE.search(s))


def stem_match(token: str, key: str) -> bool:
    """Ukrainian-inflection-tolerant match: антена ~ антени/антену, детектор ~ детектори; numbers match exactly."""
    if token == key:
        return True
    if len(key) < 5 or key[0].isdigit():
        return False
    stem = key[: max(4, len(key) - 2)]
    return token.startswith(stem) and len(token) - len(stem) <= 4  # антени → антенами


@dataclass
class QuoteLine:
    slug: str
    short_name: str
    option_ids: list[str]
    option_labels: list[str]
    qty: int
    unit_with_vat: int
    line_with_vat: int


class Catalog:
    """catalog.json + spelling synonyms: retrieval, cards and price maths."""

    def __init__(self, catalog: dict, synonyms: dict | None = None):
        self.version: str = catalog["catalog_version"]
        self.vat_policy: dict = catalog["vat_policy"]
        self.products: dict[str, dict] = {p["slug"]: self._sellable(p) for p in catalog["products"]}
        self.synonyms: dict[str, str] = {str(k).lower(): str(v or "").lower() for k, v in (synonyms or {}).items()}
        self._keys = {
            slug: (self.tokens(p["short_name"]), [str(t).lower() for t in p["tags"]],
                   [w for w in self.tokens(p["category"]["name_uk"]) if len(w) >= 4])
            for slug, p in self.products.items()
        }
        everything = json.dumps(catalog, ensure_ascii=False).lower()
        self.latin_words = set(re.findall(r"[a-z]{2,}", everything))  # product names etc. allowed in Ukrainian text
        self.with_vat_numbers = {str(p["price"]["with_vat"]) for p in catalog["products"]} | {
            str(o["price"]["with_vat"]) for p in catalog["products"] for o in p["options"]}
        names = {QUOTES_RE.sub("", n).strip() for p in self.products.values() for n in (p["short_name"], p["name_uk"])}
        self._names_re = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)

    def strip_names(self, text: str) -> str:
        """Remove product names so model numbers («Стриж 7"», «Омні 5.8») aren't checked as stated facts."""
        return self._names_re.sub(" ", QUOTES_RE.sub("", text))

    def named_in(self, text: str) -> set[str]:
        """Products whose full short name appears in the text («Сенсор S3», «щит 4м»)."""
        toks = set(self.tokens(text))
        return {slug for slug, (name_toks, _, _) in self._keys.items()
                if name_toks and all(any(stem_match(t, n) for t in toks) for n in name_toks)}

    def without_vat_numbers(self, slugs: list[str]) -> set[str]:
        """Without-VAT figures of these products/options that can't be mistaken for a with-VAT price."""
        ps = [self.products[s] for s in slugs]
        nums = {str(p["price"]["without_vat"]) for p in ps}
        nums |= {str(o["price"]["without_vat"]) for p in ps for o in p["options"]}
        return nums - self.with_vat_numbers - {"0"}

    @staticmethod
    def _sellable(p: dict) -> dict:
        """Hide price-less on-request options (a custom colour). Standard colours are free choices."""
        options = [o for o in p["options"] if "on_request" not in o["flags"]]
        groups = [g for g in p["option_groups"] if any(o["group"] == g["id"] for o in options)]
        return {**p, "options": options, "option_groups": groups}

    def tokens(self, text: str) -> list[str]:
        text = QUOTES_RE.sub(" ", text.lower().translate(APOSTROPHES))
        out = []
        for tok in TOKEN_RE.findall(text):
            tok = tok.strip(".'")
            tok = self.synonyms.get(tok, tok)
            if tok:
                out.append(tok)
        return out

    def retrieve(self, text: str, recent: list[str] | tuple = (), in_order: list[str] | tuple = (), k: int = 3) -> list[str]:
        """Score products: short-name hit +3, tag +2 each, category +1, recent +2, in the active order +5."""
        toks = set(self.tokens(text))
        scored = []
        for slug, (name_toks, tags, cat_toks) in self._keys.items():
            name_hits = sum(any(stem_match(t, n) for t in toks) for n in name_toks)
            score = 3 if name_toks and name_hits == len(name_toks) else 0
            score += 2 * sum(any(stem_match(t, tag) for t in toks) for tag in tags)
            score += 1 if any(stem_match(t, c) for t in toks for c in cat_toks) else 0
            score += 2 if slug in recent else 0
            score += 5 if slug in in_order else 0
            if score > 0:
                scored.append((-score, -name_hits, slug))
        return [slug for *_, slug in sorted(scored)[:k]]

    def photo_sources(self, slug: str) -> list[str]:
        return [x["src"] for x in self.products[slug]["photos"]]

    def all_photo_sources(self) -> list[str]:
        """Every image the bot may send (gallery photos and colour photos), without duplicates."""
        srcs = [x["src"] for p in self.products.values() for x in p["photos"]]
        srcs += [c["src"] for p in self.products.values() for c in p["colors"]]
        return list(dict.fromkeys(srcs))

    def card(self, slug: str) -> dict[str, Any]:
        """Compact product card for the prompt (~1k tokens): long texts trimmed, URLs, raw fields and tags dropped."""
        p = self.products[slug]

        def trim(s: str, n: int) -> str:
            return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"

        c = {k: p[k] for k in ("slug", "short_name", "name_uk", "category", "specs", "spec_notes",
                               "summary_uk", "box_contents", "related_models", "nato_codified") if p.get(k)}
        c["price"] = {"with_vat": p["price"]["with_vat"], "without_vat": p["price"]["without_vat"]}
        if p["options"]:
            c["option_groups"] = p["option_groups"]
            c["options"] = [{k: o[k] for k in ("id", "group", "label_uk", "price", "note_uk", "flags") if o.get(k)}
                            for o in p["options"]]
        if p["colors"]:
            c["colors"] = [{"id": x["id"], "label_uk": x["label_uk"]} for x in p["colors"]]
        c["description_uk"] = trim(clean_site_text(p.get("description_uk", "")), 700)
        for k in ("warranty_uk", "returns_uk", "delivery_uk", "payment_uk"):
            if p.get(k):
                c[k] = trim(clean_site_text(p[k]), 300)
        c["faq"] = [{"q": f["q"], "a": trim(clean_site_text(f["a"]), 300)} for f in p["faq"][:6]]
        c["photos_available"] = len(p["photos"])
        return c

    def quote(self, items: list[dict]) -> tuple[list[QuoteLine], int, list[str]]:
        """Python-side price maths for price_quote and orders. Returns (lines, total, errors)."""
        lines, errors = [], []
        if not 1 <= len(items) <= 5:
            errors.append("price_quote needs 1 to 5 items")
        for it in items:
            p = self.products.get(it.get("product_slug", ""))
            if not p:
                errors.append(f"unknown product_slug {it.get('product_slug')!r}")
                continue
            qty = 1 if it.get("qty") is None else it["qty"]
            if not isinstance(qty, int) or not 1 <= qty <= 999:
                errors.append(f"{p['slug']}: qty must be 1..999")
                continue
            opts = {o["id"]: o for o in p["options"]}
            chosen = list(dict.fromkeys(it.get("option_ids") or []))
            bad = [o for o in chosen if o not in opts]
            if bad:
                errors.append(f"{p['slug']}: unknown option ids {bad}; valid: {list(opts)}")
                continue
            exclusive = {g["id"] for g in p["option_groups"] if g["exclusive"]}
            for g in exclusive:
                same = [o for o in chosen if opts[o]["group"] == g]
                if len(same) > 1:
                    errors.append(f"{p['slug']}: options {same} exclude each other (pick one)")
            unit = p["price"]["with_vat"] + sum(opts[o]["price"]["with_vat"] for o in chosen)
            lines.append(QuoteLine(p["slug"], p["short_name"], chosen, [opts[o]["label_uk"] for o in chosen], qty, unit,
                                   unit * qty))
        return lines, sum(line.line_with_vat for line in lines), errors

    def index_lines(self) -> str:
        """Grouped by category (keeps the cached prefix small). No stock status: everything is treated as in stock."""
        out, current = [], None
        for p in sorted(self.products.values(), key=lambda p: (p["category"]["name_uk"], p["short_name"])):
            if p["category"]["name_uk"] != current:
                current = p["category"]["name_uk"]
                out.append(f"\n## {current}")
            out.append(f"{p['slug']} | {p['short_name']} | {fmt_uah(p['price']['with_vat'])}")
        return "\n".join(out).strip()
