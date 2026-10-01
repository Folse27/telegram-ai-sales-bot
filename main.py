"""Обрій Тех demo sales-manager bot: everything that runs on the server (PLAN.md §3).

Run on Render:        python main.py              (webhook, aiohttp)
Run locally (dev):    python main.py --polling    (deletes the webhook, long-polls)
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import asyncpg
import yaml
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto, LinkPreviewOptions, Message, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from config import (CATALOG_PATH, CONTENT_PATH, OVERRIDES_PATH, PHOTOS_PATH, PROMPT_PATH, REPLY_TOOL_PATH,
                    Settings, get_settings)

log = logging.getLogger("Обрій")


# ═══════════════════════════ 1. Storage ═══════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    chat_id          bigint PRIMARY KEY,
    user_id          bigint,
    username         text,
    full_name        text,
    mode             text NOT NULL DEFAULT 'BOT' CHECK (mode IN ('BOT', 'HUMAN')),
    mode_changed_at  timestamptz NOT NULL DEFAULT now(),
    last_manager_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS seen_updates (
    update_id  bigint PRIMARY KEY,
    seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id             bigserial PRIMARY KEY,
    chat_id        bigint NOT NULL REFERENCES clients (chat_id),
    role           text NOT NULL CHECK (role IN ('user', 'assistant', 'manager')),
    text           text NOT NULL,
    tg_message_id  bigint,
    answered       boolean,  -- user rows only: false until a reply batch covering it was delivered
    created_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta jsonb;  -- assistant rows: {"products": [...]} for retrieval
CREATE INDEX IF NOT EXISTS messages_chat_idx ON messages (chat_id, id);
CREATE INDEX IF NOT EXISTS messages_unanswered_idx ON messages (created_at) WHERE role = 'user' AND NOT answered;
CREATE TABLE IF NOT EXISTS orders (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    status           text NOT NULL,
    data             jsonb NOT NULL DEFAULT '{}',
    payment_sent_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_editable ON orders (chat_id)
    WHERE status IN ('DRAFT', 'AWAITING_CONFIRMATION');
CREATE TABLE IF NOT EXISTS handoffs (
    id               bigserial PRIMARY KEY,
    chat_id          bigint NOT NULL REFERENCES clients (chat_id),
    reason           text NOT NULL,
    department       text,
    summary          text,
    card_message_id  bigint,
    status           text NOT NULL DEFAULT 'open',
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz
);
CREATE TABLE IF NOT EXISTS relay_map (
    admin_message_id  bigint PRIMARY KEY,
    chat_id           bigint NOT NULL,
    handoff_id        bigint REFERENCES handoffs (id),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  bigserial PRIMARY KEY,
    chat_id             bigint,
    model               text,
    attempt             int,
    input_tokens        int,
    output_tokens       int,
    cache_read_tokens   int,
    cache_write_tokens  int,
    latency_ms          int,
    status              text,
    validation          jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
"""


async def create_pool(dsn: str, schema: str | None = None) -> asyncpg.Pool:
    async def init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def use_schema(conn: asyncpg.Connection) -> None:
        # Neon's proxy drops search_path as a startup parameter, and the pool runs RESET ALL on release,
        # so the test schema is set on every acquire.
        await conn.execute(f'SET search_path TO "{schema}"')

    # No idle connections are kept (min_size=0, closed after 60 s idle) so Neon can scale to zero.
    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=5, max_inactive_connection_lifetime=60, command_timeout=15, init=init,
        setup=use_schema if schema else None,
    )


class Storage:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._seen: OrderedDict[int, None] = OrderedDict()  # in-memory LRU in front of seen_updates

    @classmethod
    async def connect(cls, dsn: str, schema: str | None = None) -> Storage:
        """Open the pool and create tables if missing. `schema` isolates tests (one throwaway schema per case)."""
        if schema:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            finally:
                await conn.close()
        st = cls(await create_pool(dsn, schema))
        await st.pool.execute(SCHEMA_SQL)
        return st

    async def close(self) -> None:
        await self.pool.close()

    async def version(self) -> str:
        return (await self.pool.fetchval("SELECT version()")).split(",")[0]

    async def cleanup(self) -> None:
        await self.pool.execute("DELETE FROM seen_updates WHERE seen_at < now() - interval '3 days'")

    async def first_seen(self, update_id: int) -> bool:
        """True the first time an update_id arrives; Telegram retries and Render restarts can resend."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)
        row = await self.pool.fetchval(
            "INSERT INTO seen_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING update_id", update_id
        )
        return row is not None

    async def upsert_client(self, chat_id: int, user_id: int | None, username: str | None, full_name: str | None) -> None:
        await self.pool.execute(
            """INSERT INTO clients (chat_id, user_id, username, full_name) VALUES ($1, $2, $3, $4)
               ON CONFLICT (chat_id) DO UPDATE
               SET user_id = $2, username = $3, full_name = $4, last_seen_at = now()""",
            chat_id, user_id, username, full_name,
        )

    async def get_mode(self, chat_id: int) -> str:
        return await self.pool.fetchval("SELECT mode FROM clients WHERE chat_id = $1", chat_id) or "BOT"

    async def set_mode(self, chat_id: int, mode: str) -> None:
        await self.pool.execute(
            "UPDATE clients SET mode = $2, mode_changed_at = now() WHERE chat_id = $1", chat_id, mode
        )

    async def get_client(self, chat_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM clients WHERE chat_id = $1", chat_id)

    async def save_message(self, chat_id: int, role: str, text: str, tg_message_id: int | None = None,
                           answered: bool | None = None, meta: dict | None = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO messages (chat_id, role, text, tg_message_id, answered, meta) VALUES ($1, $2, $3, $4, $5, $6)"
            " RETURNING id",
            chat_id, role, text, tg_message_id, answered, meta,
        )

    async def mark_answered(self, message_ids: list[int]) -> None:
        if message_ids:
            await self.pool.execute("UPDATE messages SET answered = true WHERE id = ANY($1::bigint[])", message_ids)

    async def history(self, chat_id: int, limit: int = 20, exclude_ids: list[int] | None = None) -> list[asyncpg.Record]:
        rows = await self.pool.fetch(
            "SELECT id, role, text, meta, created_at FROM messages WHERE chat_id = $1 AND NOT (id = ANY($3::bigint[]))"
            " ORDER BY id DESC LIMIT $2",
            chat_id, limit, exclude_ids or [],
        )
        return rows[::-1]

    async def unanswered_recent(self, seconds: int = 120) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, chat_id, text, tg_message_id FROM messages WHERE role = 'user' AND NOT answered"
            " AND created_at > now() - make_interval(secs => $1) ORDER BY id", seconds,
        )

    async def log_llm_call(self, chat_id: int, model: str, attempt: int, usage: Any, latency_ms: int,
                           status: str, validation: dict) -> None:
        await self.pool.execute(
            "INSERT INTO llm_calls (chat_id, model, attempt, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, latency_ms, status, validation) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            chat_id, model, attempt, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
            getattr(usage, "cache_read_input_tokens", None), getattr(usage, "cache_creation_input_tokens", None),
            latency_ms, status, validation,
        )


# ═══════════════════════════ 2. Catalog ═══════════════════════════

TOKEN_RE = re.compile(r"[\w'.]+")
APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'"})
QUOTES_RE = re.compile(r"[\"«»“”„″]")


def fmt_uah(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def stem_match(token: str, key: str) -> bool:
    """Ukrainian-inflection-tolerant match: сенсора ~ сенсору/сенсори, детектор ~ детектори; numbers match exactly."""
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
    """catalog.json + overrides (synonyms) + photos.json; retrieval, cards and price maths."""

    def __init__(self, catalog: dict, overrides: dict, photos: dict):
        self.version = catalog["catalog_version"]
        self.vat_policy = catalog["vat_policy"]
        self.products: dict[str, dict] = {p["slug"]: self._sellable(p) for p in catalog["products"]}
        self.file_ids: dict[str, str] = {k: v["file_id"] for k, v in photos.items()}
        self.synonyms: dict[str, str] = {str(k).lower(): str(v).lower() for k, v in (overrides.get("synonyms") or {}).items()}
        self._keys = {
            slug: (self.tokens(p["short_name"]), [str(t).lower() for t in p["tags"]],
                   [w for w in self.tokens(p["category"]["name_uk"]) if len(w) >= 4])
            for slug, p in self.products.items()
        }
        everything = json.dumps(catalog, ensure_ascii=False).lower()
        self.latin_words = set(re.findall(r"[a-z]{2,}", everything))  # product names etc. allowed in Ukrainian text
        self.with_vat_numbers = {str(p["price"]["with_vat"]) for p in catalog["products"]} | {
            str(o["price"]["with_vat"]) for p in catalog["products"] for o in p["options"]}

    def strip_names(self, text: str) -> str:
        """Remove product names so model numbers («Сенсора 3.0», «Жах 7"») aren't checked as stated facts."""
        if not hasattr(self, "_names_re"):
            names = {QUOTES_RE.sub("", n).strip() for p in self.products.values() for n in (p["short_name"], p["name_uk"])}
            self._names_re = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)
        return self._names_re.sub(" ", QUOTES_RE.sub("", text))

    def named_in(self, text: str) -> set[str]:
        """Products whose full short name appears in the text («Сенсора 3.0», «щит 4м»)."""
        toks = set(self.tokens(text))
        return {slug for slug, (name_toks, _, _) in self._keys.items()
                if name_toks and all(any(stem_match(t, n) for t in toks) for n in name_toks)}

    def without_vat_numbers(self, slugs: list[str]) -> set[str]:
        """Без-ПДВ figures of these products/options that can't be mistaken for a з-ПДВ price."""
        ps = [self.products[s] for s in slugs]
        nums = {str(p["price"]["without_vat"]) for p in ps} | {str(o["price"]["without_vat"]) for p in ps for o in p["options"]}
        return nums - self.with_vat_numbers - {"0"}

    @staticmethod
    def _sellable(p: dict) -> dict:
        """Hide price-less on-request options ("Власний колір": a custom colour). Standard colours are free choices."""
        options = [o for o in p["options"] if "on_request" not in o["flags"]]
        groups = [g for g in p["option_groups"] if any(o["group"] == g["id"] for o in options)]
        return {**p, "options": options, "option_groups": groups}

    @classmethod
    def load(cls) -> Catalog:
        return cls(json.loads(CATALOG_PATH.read_text(encoding="utf-8")),
                   yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")),
                   json.loads(PHOTOS_PATH.read_text(encoding="utf-8")))

    def tokens(self, text: str) -> list[str]:
        text = QUOTES_RE.sub(" ", text.lower().translate(APOSTROPHES))
        out = []
        for tok in TOKEN_RE.findall(text):
            tok = tok.strip(".'")
            tok = self.synonyms.get(tok, tok)
            if tok:
                out.append(tok)
        return out

    def retrieve(self, text: str, recent: list[str] = (), in_order: list[str] = (), k: int = 3) -> list[str]:
        """PLAN.md §4 retrieval: short-name hit +3, tag +2 each, category +1, recent +2, in active order +5."""
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

    def card(self, slug: str) -> dict:
        """Compact product card for the prompt (~1k tokens): long texts trimmed, URLs/raw/tags dropped."""
        p = self.products[slug]
        trim = lambda s, n: s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"  # noqa: E731
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
        c["photos_available"] = sum(1 for i in range(len(p["photos"])) if f"{slug}/{i}" in self.file_ids)
        return c

    def quote(self, items: list[dict]) -> tuple[list[QuoteLine], int, list[str]]:
        """Python-side price maths for price_quote / orders. Returns (lines, total, errors)."""
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


# ═══════════════════════════ 3. Templates ═══════════════════════════

MODEL_TEMPLATES = ("product_card", "product_photos", "product_color", "price_quote", "vat_explainer",
                   "department_contact", "services_list")


@dataclass
class RenderedPart:
    """One outgoing Telegram message. `history_text` is what the LLM sees of it in later turns."""

    kind: str  # text | photo | media_group
    text: str = ""
    file_ids: list[str] = field(default_factory=list)
    history_text: str = ""
    meta: dict | None = None  # first part of a reply carries {"products": [...]} into messages.meta


def plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


class Templates:
    def __init__(self, content: dict, catalog: Catalog, persona_name: str):
        self.content, self.catalog, self.persona_name = content, catalog, persona_name
        self.t = content["templates"]

    @classmethod
    def load_content(cls) -> dict:
        return yaml.safe_load(CONTENT_PATH.read_text(encoding="utf-8"))

    def text(self, template_id: str, **values: Any) -> RenderedPart:
        """Render a text template; values are HTML-escaped unless they are already-built HTML (`*_html`)."""
        esc = {k: (v if k.endswith("_html") else html.escape(str(v))) for k, v in values.items()}
        body = self.t[template_id]["text_uk"].format(**{k.removesuffix("_html"): v for k, v in esc.items()})
        return RenderedPart("text", body, history_text=plain(body))

    def _photos(self, keys: list[str], caption: str, label: str) -> RenderedPart:
        ids = [self.catalog.file_ids[k] for k in keys if k in self.catalog.file_ids]
        if not ids:
            return RenderedPart("text", caption, history_text=plain(caption))
        kind = "photo" if len(ids) == 1 else "media_group"
        return RenderedPart(kind, caption, ids, history_text=f"[{label}: {len(ids)} фото] {plain(caption)}")

    def render(self, part: dict) -> RenderedPart:
        tid = part["template_id"]
        cat = self.catalog
        if tid in ("product_card", "product_photos", "product_color", "vat_explainer"):
            p = cat.products[part["product_slug"]]
        if tid == "product_card":
            caption = self.t[tid]["caption_uk"].format(name_uk=html.escape(p["name_uk"]))
            keys = [x["key"] for x in p["photos"]][: self.t[tid]["max_photos"]]
            return self._photos(keys, caption, "картка товару")
        if tid == "product_photos":
            keys = [x["key"] for x in p["photos"]][: self.t[tid]["max_photos"]]
            return self._photos(keys, self.t[tid]["caption_uk"].format(short_name=html.escape(p["short_name"])), "фото")
        if tid == "product_color":
            c = next(x for x in p["colors"] if x["id"] == part["color_id"])
            caption = self.t[tid]["caption_uk"].format(short_name=html.escape(p["short_name"]),
                                                       color_label=html.escape(c["label_uk"]))
            return self._photos([c["photo_key"]], caption, "фото кольору")
        if tid == "price_quote":
            lines, total, _ = cat.quote(part["items"])
            fmt_line = self.t[tid]["line_uk"]
            rendered = [fmt_line.format(
                short_name=html.escape(line.short_name),
                options_line=html.escape(f" (+ {', '.join(line.option_labels)})") if line.option_labels else "",
                qty=line.qty, line_with_vat=fmt_uah(line.line_with_vat)) for line in lines]
            return self.text(tid, lines_html="\n".join(rendered), total_with_vat=fmt_uah(total))
        if tid == "vat_explainer":
            return self.text(tid, price_without_vat=fmt_uah(p["price"]["without_vat"]))
        if tid == "department_contact":
            d = self.content["departments"][part["department"]]
            lines = "".join(f"\n{html.escape(v)}" for v in (d.get("phone"), d.get("email"), d.get("url")) if v)
            note = f"\n{html.escape(d['note_uk'])}" if d.get("note_uk") else ""
            return self.text(tid, dept_title=d["title_uk"], dept_lines_html=lines, dept_note_html=note)
        if tid == "services_list":
            return self.text(tid, services="\n".join(f"• {s}" for s in self.content["services_uk"]))
        raise ValueError(f"unknown template {tid}")


# ═══════════════════════════ 4. Validators ═══════════════════════════

PRICE_RE = re.compile(r"(\d{1,3}(?:[   ]\d{3})+|\d+)\s*грн", re.I)  # a number written as a price
NUM_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?!\d)|\d+(?:[.,]\d+)?")
URL_EMAIL_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+")
RUSSIAN_LETTERS_RE = re.compile(r"[ыэъёЫЭЪЁ]")
UKRAINIAN_LETTERS_RE = re.compile(r"[іїєґІЇЄҐ]")
IDENTITY_OK_RE = re.compile(r"бот|штучн|ШІ", re.I)
HUMAN_CLAIM_RE = re.compile(r"\bя\s+(жива\s+)?людина\b", re.I)


def numbers_in(text: str) -> set[str]:
    return {re.sub(r"[   ]", "", m).replace(",", ".") for m in NUM_RE.findall(URL_EMAIL_RE.sub(" ", text))}


def reference_numbers(system: list[dict]) -> set[str]:
    """Numbers from the cached prompt that may appear in replies: prices and decimals, not small integers.
    Small integers there are model names (Жах 7", кабель 3 м) and would whitelist invented days or quantities."""
    return {n for n in numbers_in(system[0]["text"] + system[1]["text"]) if "." in n or len(n) >= 3}


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)
    drop_parts: set[int] = field(default_factory=set)  # payment-leak parts: removed, not retried
    force_escalate: str | None = None


class Validator:
    """PLAN.md §5 post-generation checks. Text parts are checked; template output is Python's and trusted."""

    def __init__(self, catalog: Catalog, content: dict):
        self.catalog = catalog
        pay = content["templates"]["payment_details"]["text_uk"]
        self.payment_markers = [m for m in re.findall(r"UA\d{27}|\d{10}", pay)] + ["ДЕМО-ОТРИМУВАЧ", "ДЕМО-БАНК"]
        self.departments = set(content["departments"])

    def language_errors(self, text: str) -> list[str]:
        errs = []
        if RUSSIAN_LETTERS_RE.search(text):
            errs.append("uses Russian letters (ы/э/ъ/ё): write in Ukrainian only")
        words = re.findall(r"[^\W\d_]+", URL_EMAIL_RE.sub(" ", text))
        cyr = sum(len(w) for w in words if re.match(r"[а-яіїєґ']", w, re.I))
        lat = sum(len(w) for w in words if re.match(r"[a-z]", w, re.I) and w.lower() not in self.catalog.latin_words)
        if cyr + lat and cyr / (cyr + lat) < 0.8:
            errs.append("text is not in Ukrainian")
        if len(text) > 40 and not UKRAINIAN_LETTERS_RE.search(text):
            errs.append("text looks Russian (no і/ї/є/ґ): write natural Ukrainian")
        return errs

    def check(self, data: dict, allowed_numbers: set[str], without_vat: set[str] = frozenset(),
              discussed: set[str] | None = None) -> Check:
        """`discussed`: products the client named now or the last reply was about. The vat_explainer quotes a
        price, so it is only accepted for one of them (None = no restriction, used by unit tests of other checks)."""
        ch, cat = Check(), self.catalog
        parts = data.get("parts") or []
        if not 1 <= len(parts) <= 3:
            ch.errors.append(f"parts must contain 1 to 3 items, got {len(parts)}")
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
        if tid not in MODEL_TEMPLATES:
            return [f"template_id {tid!r} is not allowed"]
        if tid in ("product_card", "product_photos", "product_color", "vat_explainer"):
            slug = part.get("product_slug")
            if slug not in cat.products:
                return [f"{tid}: unknown product_slug {slug!r} (use a slug from the catalog index)"]
            if tid == "product_color" and part.get("color_id") not in {c["id"] for c in cat.products[slug]["colors"]}:
                return [f"product_color: colour {part.get('color_id')!r} not in {[c['id'] for c in cat.products[slug]['colors']]}"]
        if tid == "department_contact" and part.get("department") not in self.departments:
            return [f"department_contact: unknown department {part.get('department')!r}"]
        if tid == "price_quote":
            return cat.quote(part.get("items") or [])[2]
        return []

    def _payment_leak(self, text: str) -> bool:
        return bool(re.search(r"UA\d{27}|\b(?:\d[ -]?){16}\b", text)
                    or any(m.lower() in text.lower() for m in self.payment_markers)
                    or re.search(r"реквізит\w*[^\n]{0,40}\d{6,}", text, re.I))


# ═══════════════════════════ 5. LLM ═══════════════════════════

def model_params(model: str) -> dict:
    """Per-family request differences (sampling params and thinking were removed on newer models)."""
    if model.startswith(("claude-haiku-4-5", "claude-3", "claude-sonnet-4-5", "claude-opus-4-5", "claude-opus-4-1")):
        return {"temperature": None}  # filled from settings
    if model.startswith(("claude-sonnet-5", "claude-opus-5", "claude-opus-4-")) and not model.startswith("claude-opus-5-5"):
        return {"thinking": {"type": "disabled"}}  # forced tool_choice can't be combined with thinking
    return {"tool_choice_auto": True}  # Fable 5.1 / Opus 5.5 / unknown: forced tool_choice is a 400


class LLM:
    def __init__(self, settings: Settings, catalog: Catalog, content: dict, storage: Storage):
        import anthropic  # imported here so unit tests of other sections don't need the SDK configured

        self.settings, self.catalog, self.content, self.storage = settings, catalog, content, storage
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None,
                                               max_retries=2, timeout=settings.llm_timeout)
        self.tool = json.loads(REPLY_TOOL_PATH.read_text(encoding="utf-8"))
        self.system = build_system(settings, catalog, content)
        self.reference_numbers = reference_numbers(self.system)
        self.params = model_params(settings.model)
        if "temperature" in self.params:
            self.params["temperature"] = settings.llm_temperature

    def request(self, messages: list[dict]) -> dict:
        req = {"model": self.settings.model, "max_tokens": self.settings.llm_max_tokens, "system": self.system,
               "tools": [self.tool], "messages": messages}
        params = dict(self.params)
        if params.pop("tool_choice_auto", False):
            req["tool_choice"] = {"type": "auto"}
        else:
            req["tool_choice"] = {"type": "tool", "name": "reply"}
        if "temperature" in params:  # SDK 1.x dropped the kwarg; models before 4.6 (Haiku 4.5) still honour it
            req["extra_body"] = {"temperature": params.pop("temperature")}
        req.update(params)
        return req

    async def call(self, chat_id: int, messages: list[dict], attempt: int) -> tuple[Any, dict | None, int]:
        t0 = time.monotonic()
        resp = await self.client.messages.create(**self.request(messages))
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == "reply"), None)
        latency = int((time.monotonic() - t0) * 1000)
        log.info("llm chat=%s attempt=%d %dms in=%s out=%s cache_read=%s cache_write=%s", chat_id, attempt, latency,
                 resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_input_tokens,
                 resp.usage.cache_creation_input_tokens)
        return resp, (block and {"id": block.id, "input": block.input}), latency


SITE_ONLY_RE = re.compile(r"наприкінці сторінки|«Часті запитання»|зверн\w*\s+до\s+менеджер", re.I)


def clean_site_text(text: str) -> str:
    """Drop website-navigation sentences and "contact Обрій managers" (the real company) from scraped text."""
    return " ".join(s for s in re.split(r"(?<=[.!?])\s+", text) if not SITE_ONLY_RE.search(s))


def build_system(settings: Settings, catalog: Catalog, content: dict) -> list[dict]:
    """Two static system blocks; the second ends the cached prefix (tools + system). Nothing volatile in here."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    rules, reference = raw.split("<!-- REFERENCE -->", 1)
    deps = []
    for key, d in content["departments"].items():
        contacts = ", ".join(v for v in (d.get("phone"), d.get("email"), d.get("url")) if v)
        deps.append(f"- {key}: {d['title_uk']} — {contacts}" + (f" ({d['note_uk']})" if d.get("note_uk") else ""))
    vat = [clean_site_text(catalog.vat_policy["without_vat_condition_uk"])] + [
        f"- {f['q']} {clean_site_text(f['a'])}" for f in catalog.vat_policy["faq"]]
    values = {
        "persona_name": settings.persona_name,
        "catalog_version": catalog.version,
        "catalog_index": catalog.index_lines(),
        "departments": "\n".join(deps),
        "services": "\n".join(f"- {s}" for s in content["services_uk"]),
        "policies": "\n".join(f"- {v}" for v in content["policies_uk"].values()),
        "vat_policy": "\n".join(vat),
    }
    for k, v in values.items():
        rules, reference = rules.replace("{{" + k + "}}", v), reference.replace("{{" + k + "}}", v)
    return [{"type": "text", "text": rules.strip()},
            {"type": "text", "text": reference.strip(), "cache_control": {"type": "ephemeral"}}]


# ═══════════════════════════ 6. Orders ═══════════════════════════
# Step 6: order state machine (PLAN.md §7).


# ═══════════════════════════ 7. Handoff ═══════════════════════════
# Step 6: admin card, buttons, relay, admin commands (PLAN.md §8).


# ═══════════════════════════ 8. Sessions ═══════════════════════════

IDLE, COLLECTING, GENERATING, SENDING = "IDLE", "COLLECTING", "GENERATING", "SENDING"
TYPING_EVERY = 4.0  # Telegram shows "typing…" for ~5 s per chat action
SESSION_IDLE_EVICT = 1800.0


class Clock:
    """Injectable time source: tests swap in a fake clock that advances virtually."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


@dataclass
class Incoming:
    chat_id: int
    text: str
    db_id: int | None = None
    tg_id: int = 0  # Telegram message_id: orders a batch when background handlers race


@dataclass
class Reply:
    parts: list[Any]  # opaque to the session; Pipeline.send_part knows how to deliver each one


@dataclass
class SessionConfig:
    debounce: float = 10.0
    max_wait: float = 25.0
    gap_min: float = 1.0
    gap_max: float = 3.0

    @classmethod
    def from_settings(cls, s: Settings) -> SessionConfig:
        return cls(s.debounce_seconds, s.debounce_max_wait, s.part_gap_min, s.part_gap_max)


class Pipeline(Protocol):
    """What a session needs from the rest of the bot (LLM pipeline from Step 5; echo until then)."""

    async def is_human(self, chat_id: int) -> bool: ...
    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None: ...
    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply: ...
    async def send_part(self, chat_id: int, part: Any) -> None: ...
    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[Any]) -> None: ...
    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None: ...


@dataclass
class SessionStats:
    calls: int = 0
    cancels: int = 0
    replies: int = 0


class ChatSession:
    """Per-chat debounce state machine (PLAN.md §6): IDLE → COLLECTING → GENERATING → SENDING."""

    def __init__(self, chat_id: int, pipeline: Pipeline, transport: Transport, cfg: SessionConfig,
                 clock: Clock, rng: random.Random | None = None):
        self.chat_id, self.pipeline, self.transport, self.cfg, self.clock = chat_id, pipeline, transport, cfg, clock
        self.rng = rng or random.Random()
        self.state = IDLE
        self.buffer: list[Incoming] = []  # messages not yet handed to generation
        self.first_at: float | None = None  # arrival of the first buffered message
        self.last_at: float | None = None
        self.batch: list[Incoming] = []  # what the running generation is answering
        self.batch_first_at: float | None = None
        self.last_activity = clock.now()
        self.stats = SessionStats()
        self.lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._gen: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # strong refs so running tasks aren't garbage-collected

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def on_message(self, msg: Incoming) -> None:
        if await self.pipeline.is_human(self.chat_id):  # HUMAN mode: no timer, no LLM
            await self.pipeline.relay_to_human(self.chat_id, [msg])
            return
        async with self.lock:
            now = self.last_activity = self.clock.now()
            self.buffer.append(msg)
            self.last_at = now
            if self.first_at is None:
                self.first_at = now
            if self.state in (IDLE, COLLECTING):
                self.state = COLLECTING
                self._arm()
            elif self.state == GENERATING and now - self.batch_first_at <= self.cfg.max_wait:
                # Nothing sent yet: drop this generation and answer everything together.
                self._gen.cancel()
                self._gen = None
                self.stats.cancels += 1
                self.buffer = self.batch + self.buffer
                self.first_at, self.batch, self.batch_first_at = self.batch_first_at, [], None
                self.state = COLLECTING
                self._arm()
            # GENERATING past MAX_WAIT, or SENDING: the message waits in the buffer for the next batch.

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        deadline = min(self.last_at + self.cfg.debounce, self.first_at + self.cfg.max_wait)
        self._timer = self._spawn(self._fire_at(deadline))

    async def _fire_at(self, deadline: float) -> None:
        await self.clock.sleep(deadline - self.clock.now())
        async with self.lock:
            if self.state != COLLECTING or not self.buffer or self._timer is not asyncio.current_task():
                return
            self._timer = None
            self.batch = sorted(self.buffer, key=lambda m: m.tg_id)
            self.batch_first_at = self.first_at
            self.buffer, self.first_at, self.last_at = [], None, None
            self.state = GENERATING
            self._gen = self._spawn(self._generate_and_send(self.batch))

    async def _generate_and_send(self, batch: list[Incoming]) -> None:
        typing = self._spawn(self._typing_loop())
        try:
            self.stats.calls += 1
            reply = await self.pipeline.generate(self.chat_id, batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("generation failed for chat %s", self.chat_id)
            typing.cancel()
            async with self.lock:
                if self._gen is asyncio.current_task():
                    self._gen, self.state = None, SENDING  # a message now can't cancel the error reply
            try:
                await self.pipeline.on_error(self.chat_id, batch, exc)
            except Exception:
                log.exception("on_error failed for chat %s", self.chat_id)
            await self._finish()
            return
        finally:
            typing.cancel()

        async with self.lock:
            if self._gen is not asyncio.current_task():  # cancelled while waiting for the lock
                return
            self._gen = None
            self.state = SENDING
        sent = await self._send(reply)
        try:
            await self.pipeline.commit(self.chat_id, batch, sent)  # history gets only what was really sent
        except Exception:
            log.exception("commit failed for chat %s", self.chat_id)
        self.stats.replies += 1
        await self._finish()

    async def _typing_loop(self) -> None:
        while True:
            await self.transport.send_typing(self.chat_id)
            await self.clock.sleep(TYPING_EVERY)

    async def _send(self, reply: Reply) -> list[Any]:
        sent = []
        for i, part in enumerate(reply.parts):
            if i:  # the first part goes out at once: the debounce window already made the client wait
                await self.transport.send_typing(self.chat_id)
                await self.clock.sleep(self.rng.uniform(self.cfg.gap_min, self.cfg.gap_max))
            try:
                await self.pipeline.send_part(self.chat_id, part)
                sent.append(part)
            except Exception:
                log.exception("sending part %d failed for chat %s", i, self.chat_id)
        return sent

    async def _finish(self) -> None:
        async with self.lock:
            self.batch, self.batch_first_at = [], None
            self.last_activity = self.clock.now()
            if self.buffer:  # arrived during GENERATING/SENDING: their timer runs from arrival, fires now at the earliest
                self.state = COLLECTING
                self._arm()
            else:
                self.state = IDLE


class SessionManager:
    def __init__(self, pipeline: Pipeline, transport: Transport, cfg: SessionConfig, clock: Clock | None = None):
        self.pipeline, self.transport, self.cfg = pipeline, transport, cfg
        self.clock = clock or Clock()
        self.sessions: dict[int, ChatSession] = {}

    def get(self, chat_id: int) -> ChatSession:
        now = self.clock.now()
        for cid in [c for c, s in self.sessions.items() if s.state == IDLE and now - s.last_activity > SESSION_IDLE_EVICT]:
            del self.sessions[cid]
        if chat_id not in self.sessions:
            self.sessions[chat_id] = ChatSession(chat_id, self.pipeline, self.transport, self.cfg, self.clock)
        return self.sessions[chat_id]

    async def on_message(self, msg: Incoming) -> None:
        await self.get(msg.chat_id).on_message(msg)


class LlmPipeline:
    """The real pipeline (PLAN.md §5): retrieval → prompt → Haiku `reply` call → validators (retry once) → templates."""

    def __init__(self, settings: Settings, storage: Storage, transport: Transport, catalog: Catalog,
                 templates: Templates, validator: Validator, llm: LLM):
        self.settings, self.storage, self.transport = settings, storage, transport
        self.catalog, self.templates, self.validator, self.llm = catalog, templates, validator, llm

    async def is_human(self, chat_id: int) -> bool:
        return await self.storage.get_mode(chat_id) == "HUMAN"

    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None:
        log.info("chat %s is in HUMAN mode; relay arrives in Step 6", chat_id)

    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply:
        hist = await self.storage.history(chat_id, limit=self.settings.history_turns * 2,
                                          exclude_ids=[m.db_id for m in batch if m.db_id])
        with_meta = [r for r in hist if r["role"] == "assistant" and r["meta"]]
        recent = [s for r in with_meta[-6:] for s in r["meta"].get("products", [])]
        last = [s for s in (with_meta[-1]["meta"].get("products", []) if with_meta else []) if s in self.catalog.products][:2]
        batch_text = "\n".join(m.text for m in batch)
        # Follow-ups ("а гарантія на нього?") refer to the last reply's products: they keep their card slots.
        slugs = (last + [s for s in self.catalog.retrieve(batch_text, recent=recent) if s not in last])[:3]
        cards = [self.catalog.card(s) for s in slugs]
        cards_json = "\n".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in cards)
        client = await self.storage.get_client(chat_id)
        flags = {"first_contact": not any(r["role"] == "assistant" for r in hist),
                 "client_name": client["full_name"] if client else None}

        allowed = (self.llm.reference_numbers | numbers_in(cards_json) | numbers_in(batch_text)
                   | numbers_in("\n".join(r["text"] for r in hist)) | {"1"})
        for s in slugs:  # base price + any single option is a total the model may state
            p = self.catalog.products[s]
            allowed |= {str(p["price"]["with_vat"] + o["price"]["with_vat"]) for o in p["options"]}

        messages = self._history_messages(hist) + [{"role": "user", "content": (
            f"<catalog_cards>\n{cards_json or 'none matched: rely on the catalog index, ask or say it is not in the catalog'}\n"
            f"</catalog_cards>\n<order_state>none</order_state>\n"
            f"<conversation_flags>{json.dumps(flags, ensure_ascii=False)}</conversation_flags>\n"
            f"{self._client_block(batch_text)}")}]

        for attempt in (1, 2):
            resp, call, latency = await self.llm.call(chat_id, messages, attempt)
            data = call["input"] if call else None
            discussed = set(last) | self.catalog.named_in(batch_text)
            check = (self.validator.check(data, allowed, self.catalog.without_vat_numbers(slugs), discussed) if data
                     else Check(["no `reply` tool call"]))
            await self.storage.log_llm_call(chat_id, self.settings.model, attempt, resp.usage, latency,
                                            "ok" if not check.errors else "invalid",
                                            {"errors": check.errors, "dropped": sorted(check.drop_parts),
                                             "cards": slugs, "intent": data and data.get("intent"),
                                             "parts": data.get("parts") if data and check.errors else None})
            if not check.errors:
                return self._build(data, check)
            log.warning("chat %s attempt %d rejected: %s", chat_id, attempt, check.errors)
            if attempt == 1 and call:
                messages = messages + [
                    {"role": "assistant", "content": [{"type": "tool_use", "id": call["id"], "name": "reply",
                                                       "input": call["input"]}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                                                  "content": "Rejected, nothing was sent. Fix these problems and call "
                                                             "`reply` again:\n- " + "\n- ".join(check.errors)}]},
                ]
        part = self.templates.text("fallback_handoff")
        part.meta = {"products": [], "escalate": {"needed": True, "reason": "validator_failed"}}
        return Reply([part])

    def _client_block(self, text: str) -> str:
        safe = text.replace("<", "‹").replace(">", "›")  # the client can't close or forge our tags
        return f'<client_messages untrusted="true">\n{safe}\n</client_messages>'

    def _history_messages(self, rows: list) -> list[dict]:
        msgs: list[dict] = []
        for r in rows:
            role = "user" if r["role"] == "user" else "assistant"
            if r["role"] == "user":
                text = self._client_block(r["text"])
            elif r["role"] == "manager":
                text = f"[Відповідь менеджера-людини]: {r['text']}"
            else:
                text = r["text"]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        msgs = msgs[-self.settings.history_turns:]
        while msgs and msgs[0]["role"] == "assistant":  # the conversation must start with the user
            msgs.pop(0)
        return msgs

    def _build(self, data: dict, check: Check) -> Reply:
        parts = []
        for i, part in enumerate(data["parts"]):
            if i in check.drop_parts:
                continue
            if part["kind"] == "text":
                text = part["text"].strip()
                parts.append(RenderedPart("text", html.escape(text), history_text=text))
            else:
                parts.append(self.templates.render(part))
        if not parts:
            parts = [self.templates.text("fallback_handoff")]
        escalate = data["escalate"]
        if check.force_escalate:
            escalate = {**escalate, "needed": True, "reason": check.force_escalate}
        parts[0].meta = {"products": [s for s in data["products_referenced"] if s in self.catalog.products],
                         "intent": data["intent"], "escalate": escalate, "order_update": data["order_update"]}
        if escalate.get("needed") or data["order_update"]["action"] != "none":
            log.info("escalate/order (handled in Step 6): %s / %s", escalate, data["order_update"]["action"])
        return Reply(parts)

    async def send_part(self, chat_id: int, part: RenderedPart) -> None:
        if part.kind == "photo":
            await self.transport.send_photo(chat_id, part.file_ids[0], caption=part.text)
        elif part.kind == "media_group":
            await self.transport.send_media_group(chat_id, part.file_ids, caption=part.text)
        else:
            await self.transport.send_text(chat_id, part.text)

    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[RenderedPart]) -> None:
        for part in sent_parts:
            await self.storage.save_message(chat_id, "assistant", part.history_text, meta=part.meta)
        await self.storage.mark_answered([m.db_id for m in batch if m.db_id])

    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None:
        part = self.templates.text("fallback_handoff")
        await self.send_part(chat_id, part)
        await self.commit(chat_id, batch, [part])


# ═══════════════════════════ 9. Telegram ═══════════════════════════

class Transport(Protocol):
    """Everything the bot sends goes through here; tests pass a FakeTransport that records calls."""

    async def send_text(self, chat_id: int, text: str, *, reply_markup: Any = None, reply_to: int | None = None) -> int: ...
    async def send_photo(self, chat_id: int, file_id: str, caption: str | None = None) -> int: ...
    async def send_media_group(self, chat_id: int, file_ids: list[str], caption: str | None = None) -> list[int]: ...
    async def send_typing(self, chat_id: int) -> None: ...
    async def copy_message(self, to_chat_id: int, from_chat_id: int, message_id: int, reply_to: int | None = None) -> int: ...


class BotTransport:
    """Real Telegram via aiogram. Texts and captions are HTML; link previews are off."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _call(self, fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            return await fn(*args, **kwargs)
        except TelegramForbiddenError:
            log.warning("chat %s blocked the bot", args[0] if args else kwargs.get("chat_id"))
            return None

    async def send_text(self, chat_id, text, *, reply_markup=None, reply_to=None) -> int:
        m = await self._call(self.bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=reply_markup,
                             reply_to_message_id=reply_to, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return m.message_id if m else 0

    async def send_photo(self, chat_id, file_id, caption=None) -> int:
        m = await self._call(self.bot.send_photo, chat_id, file_id, caption=caption, parse_mode="HTML")
        return m.message_id if m else 0

    async def send_media_group(self, chat_id, file_ids, caption=None) -> list[int]:
        media = [InputMediaPhoto(media=f, caption=caption if i == 0 else None, parse_mode="HTML")
                 for i, f in enumerate(file_ids)]
        msgs = await self._call(self.bot.send_media_group, chat_id, media)
        return [m.message_id for m in msgs or []]

    async def send_typing(self, chat_id) -> None:
        await self._call(self.bot.send_chat_action, chat_id, "typing")

    async def copy_message(self, to_chat_id, from_chat_id, message_id, reply_to=None) -> int:
        m = await self._call(self.bot.copy_message, to_chat_id, from_chat_id, message_id, reply_to_message_id=reply_to)
        return m.message_id if m else 0


@dataclass
class Runtime:
    """Everything handlers need; put into the dispatcher as dp["rt"]."""

    settings: Settings
    storage: Storage
    transport: Transport
    sessions: SessionManager
    templates: Templates

    def allowed(self, user_id: int) -> bool:
        return not self.settings.allowed_user_ids or user_id in self.settings.allowed_user_ids


async def build_runtime(settings: Settings, bot: Bot) -> Runtime:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is empty")
    storage = await Storage.connect(settings.database_url)
    await storage.cleanup()
    log.info("database ok: %s", await storage.version())
    transport = BotTransport(bot)
    catalog, content = Catalog.load(), Templates.load_content()
    templates = Templates(content, catalog, settings.persona_name)
    llm = LLM(settings, catalog, content, storage)
    pipeline = LlmPipeline(settings, storage, transport, catalog, templates, Validator(catalog, content), llm)
    sessions = SessionManager(pipeline, transport, SessionConfig.from_settings(settings))
    log.info("catalog %s: %d products, %d photos; model %s", catalog.version, len(catalog.products),
             len(catalog.file_ids), settings.model)
    return Runtime(settings, storage, transport, sessions, templates)


async def reschedule_unanswered(rt: Runtime) -> None:
    """After a restart, answer user messages that arrived in the last 2 min but were never answered (PLAN.md §6)."""
    rows = await rt.storage.unanswered_recent(120)
    for r in rows:
        await rt.sessions.on_message(Incoming(r["chat_id"], r["text"], r["id"], r["tg_message_id"] or 0))
    if rows:
        log.info("rescheduled %d unanswered message(s)", len(rows))


router = Router()


async def _accept(message: Message, event_update: Update, rt: Runtime) -> bool:
    """Allowlist + update dedup + client upsert, shared by all private-chat handlers."""
    user = message.from_user
    if not rt.allowed(user.id) or not await rt.storage.first_seen(event_update.update_id):
        return False
    await rt.storage.upsert_client(message.chat.id, user.id, user.username, user.full_name)
    return True


@router.message(F.chat.type == "private", CommandStart())
async def on_start(message: Message, event_update: Update, rt: Runtime) -> None:
    if not await _accept(message, event_update, rt):
        return
    for tid in ("demo_disclaimer", "greeting"):
        part = rt.templates.text(tid, persona_name=rt.settings.persona_name) if tid == "greeting" else rt.templates.text(tid)
        await rt.transport.send_text(message.chat.id, part.text)
        if tid == "greeting":  # so the model knows it already introduced itself
            await rt.storage.save_message(message.chat.id, "assistant", part.history_text)


@router.message(F.chat.type == "private", F.text)
async def on_private_text(message: Message, event_update: Update, rt: Runtime) -> None:
    if not await _accept(message, event_update, rt):
        return
    db_id = await rt.storage.save_message(message.chat.id, "user", message.text, message.message_id, answered=False)
    await rt.sessions.on_message(Incoming(message.chat.id, message.text, db_id, message.message_id))


@router.message(F.chat.type == "private")
async def on_private_other(message: Message, event_update: Update, rt: Runtime) -> None:
    """Photos, voice, stickers…: the bot reads text only."""
    if await _accept(message, event_update, rt):
        await rt.transport.send_text(message.chat.id, rt.templates.text("unsupported_media").text)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


# ═══════════════════════════ 10. App ═══════════════════════════

class AccessLogger(AbstractAccessLogger):
    """Masks the webhook secret in the path and skips health-check noise (Render + UptimeRobot)."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if request.path == "/healthz":
            return
        path = "/tg/***" if request.path.startswith("/tg/") else request.path
        self.logger.info("%s %s %s %.0fms", request.method, path, response.status, time * 1000)


async def healthz(request: web.Request) -> web.Response:
    # Never touch the DB here: UptimeRobot hits this every 5 min and Neon must be able to sleep.
    return web.Response(text="ok")


def build_app(settings: Settings, bot: Bot, dp: Dispatcher) -> web.Application:
    if not settings.webhook_base_url:
        raise RuntimeError("PUBLIC_URL / RENDER_EXTERNAL_URL is empty; use --polling for local runs")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    # Checks X-Telegram-Bot-Api-Secret-Token and answers 200 right away (handling runs in background).
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.webhook_secret).register(
        app, path=settings.webhook_path
    )

    async def on_startup(app: web.Application) -> None:
        dp["rt"] = await build_runtime(settings, bot)
        await reschedule_unanswered(dp["rt"])
        await bot.set_webhook(
            settings.webhook_base_url + settings.webhook_path,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("webhook set: %s/tg/***", settings.webhook_base_url)

    async def on_cleanup(app: web.Application) -> None:
        if "rt" in dp.workflow_data:
            await dp["rt"].storage.close()
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def run_polling(settings: Settings, bot: Bot, dp: Dispatcher) -> None:
    rt = dp["rt"] = await build_runtime(settings, bot)
    await reschedule_unanswered(rt)
    try:
        await bot.delete_webhook()
        log.info("webhook deleted, polling. To hand back to Render: python scrape.py webhook set")
        await dp.start_polling(bot)
    finally:
        await rt.storage.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--polling", action="store_true", help="local dev: delete the webhook and long-poll")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    bot = Bot(settings.telegram_bot_token)
    dp = build_dispatcher()
    if args.polling:
        try:
            asyncio.run(run_polling(settings, bot, dp))
        except KeyboardInterrupt:  # Ctrl+C on Windows surfaces here
            log.info("stopped")
    else:
        web.run_app(
            build_app(settings, bot, dp), host="0.0.0.0", port=settings.port, access_log_class=AccessLogger
        )


if __name__ == "__main__":
    main()
