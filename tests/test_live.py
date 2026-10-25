"""Fake-client harness: the real pipeline and the real model, with a fake Telegram, a fake staff group and a
throwaway schema in the test database. Scripted conversations live in tests/cases.yaml (sample shop).

It spends API money, so it only runs when asked (it needs a complete .env plus TEST_DATABASE_URL):
    CMD:       set RUN_LIVE=1 && python -m pytest tests/test_live.py -q -s
    Git Bash:  RUN_LIVE=1 python -m pytest tests/test_live.py -q -s
Options: LIVE_RUNS=3 (runs per case; a case passes only if all pass) · LIVE_CASES=TC01,TC09 (subset) ·
         LIVE_BUDGET=4 (USD; remaining runs are skipped once the total passes it) ·
         LIVE_SHOP=data/sample and LIVE_CASES_FILE=tests/cases.yaml (another shop folder and its cases).
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import ADMIN, FakeTransport, TestEnv

from salesbot.catalog import Catalog
from salesbot.config import BASE_DIR, get_settings
from salesbot.handoff import Handoff
from salesbot.llm import LLM, llm_call_cost
from salesbot.orders import Orders
from salesbot.photos import PhotoCache
from salesbot.pipeline import LlmPipeline
from salesbot.sessions import IDLE, Incoming, SessionConfig, SessionManager, SessionStats
from salesbot.shop import ShopData
from salesbot.storage import Storage
from salesbot.templates import Templates
from salesbot.validation import IBAN_RE, PRICE_RE, Validator, numbers_in

SHOP_DIR = BASE_DIR / os.environ.get("LIVE_SHOP", "data/sample")
CASES_FILE = BASE_DIR / os.environ.get("LIVE_CASES_FILE", "tests/cases.yaml")
RUNS = int(os.environ.get("LIVE_RUNS", "3"))
BUDGET = float(os.environ.get("LIVE_BUDGET", "4"))
SELECT = {c.strip() for c in os.environ.get("LIVE_CASES", "").split(",") if c.strip()}
LIVE = os.environ.get("RUN_LIVE") == "1"
CASES = yaml.safe_load(Path(CASES_FILE).read_text(encoding="utf-8"))["cases"] if LIVE else []
SPENT = {"usd": 0.0, "calls": 0}
RESULTS: dict[str, list[tuple[bool, list[str]]]] = {}

pytestmark = pytest.mark.skipif(not LIVE, reason="live harness: set RUN_LIVE=1 (calls the real model, costs money)")


async def no_download(src: str) -> bytes:
    return b"image"  # the fake Telegram never looks at the bytes


class World:
    """One chat against the real pipeline; records everything the bot sends."""

    chat = 555

    async def start(self) -> None:
        s = get_settings().model_copy(update={"debounce_seconds": 0.3, "debounce_max_wait": 2.0, "part_gap_min": 0.0,
                                              "part_gap_max": 0.0, "daily_replies_per_user": 0, "daily_budget_usd": 0})
        self.settings = s
        self.schema = f"t_{uuid.uuid4().hex[:10]}"
        self.st = await Storage.connect(TestEnv().test_database_url, schema=self.schema)
        self.tr = FakeTransport()
        shop = ShopData.from_folder(SHOP_DIR)
        self.cat = Catalog(shop.catalog, shop.synonyms)
        tpl = Templates(shop.content, self.cat, s.persona_name)
        self.val = Validator(self.cat, shop.content, s.max_parts)
        orders = Orders(self.cat, tpl)
        self.ho = Handoff(SimpleNamespace(admin_chat_id=ADMIN), self.st, self.tr, tpl, orders)
        self.pl = LlmPipeline(s, self.st, self.tr, self.cat, tpl, self.val, LLM(s, self.cat, shop.content, shop.examples),
                              orders, self.ho, PhotoCache(self.st, fetch=no_download))
        self.sent: list[SimpleNamespace] = []
        real_send = self.pl.send_part

        async def spy(chat_id: int, part: Any) -> None:  # which template each client message came from
            self.sent.append(SimpleNamespace(template_id=part.template_id, kind=part.kind, text=part.text,
                                             photos=list(part.photos)))
            await real_send(chat_id, part)

        self.pl.send_part = spy  # type: ignore[method-assign]
        self.sm = SessionManager(self.pl, self.tr, SessionConfig.from_settings(s))

        async def resume(chat_id: int) -> None:
            for r in await self.ho.trailing_client_messages(chat_id):
                await self.sm.on_message(Incoming(chat_id, r["text"], r["id"], r["tg_message_id"] or 0))

        self.ho.on_resume = resume
        self.tg = 0
        await self.st.upsert_client(self.chat, self.chat, "tester", "Тест")

    async def close(self) -> None:
        await self.st.pool.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.st.close()

    async def say(self, text: str) -> None:
        self.tg += 1
        db = await self.st.save_message(self.chat, "user", text, self.tg, answered=False)
        await self.sm.on_message(Incoming(self.chat, text, db, self.tg))

    def session(self):
        return self.sm.sessions.get(self.chat)

    async def idle(self, limit_s: float = 90) -> None:
        await asyncio.sleep(0.05)
        for _ in range(int(limit_s / 0.05)):
            s = self.session()
            if s is None or (s.state == IDLE and not s.buffer):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("session did not become idle")

    async def generating(self, limit_s: float = 20) -> None:
        for _ in range(int(limit_s / 0.02)):
            s = self.session()
            if s and s.state == "GENERATING":
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("session never started generating")

    async def admin(self, action: str) -> None:
        h = await self.st.pool.fetchrow(
            "SELECT id, card_message_id FROM handoffs WHERE chat_id = $1 ORDER BY id DESC LIMIT 1", self.chat)
        if action == "paid":
            oid = await self.st.pool.fetchval("SELECT max(id) FROM orders WHERE chat_id = $1", self.chat)
            await self.ho.command(f"/paid {oid}")
        elif action in ("take", "bot", "close"):
            await self.ho.button(h["id"], action, "Тестовий менеджер")
        elif action.startswith("reply:"):
            await self.ho.manager_reply(h["card_message_id"], 990000 + self.tg, action.removeprefix("reply:"))

    async def calls(self) -> list:
        return await self.st.pool.fetch("SELECT * FROM llm_calls ORDER BY id")

    def photo_src(self, key: str) -> str | None:
        for p in self.cat.products.values():
            for x in p["photos"]:
                if x["key"] == key:
                    return x["src"]
            for c in p["colors"]:
                if c["photo_key"] == key:
                    return c["src"]
        return None

    # ── checks ──
    async def check(self, exp: dict, start: int, calls_before: int) -> list[str]:
        fails: list[str] = []
        client = [c for c in self.tr.calls[start:] if c[0] in ("text", "photos") and c[1] == self.chat]
        text = "\n".join(c[2] if c[0] == "text" else (c[3] or "") for c in client)
        admin = "\n".join(c[2] for c in self.tr.calls[start:] if c[0] == "text" and c[1] == ADMIN)
        free = "\n".join(p.text for p in self.sent if p.template_id is None and p.kind == "text")
        tids = [p.template_id for p in self.sent if p.template_id]
        for rx in exp.get("contains", []):
            if not re.search(rx, text, re.I):
                fails.append(f"missing /{rx}/")
        for rx in exp.get("not_contains", []):
            if re.search(rx, text, re.I):
                fails.append(f"forbidden /{rx}/ found")
        for rx in exp.get("free_not_contains", []):
            if re.search(rx, free, re.I):
                fails.append(f"model text has forbidden /{rx}/")
        fails += [f"template {t} not sent (sent: {tids})" for t in exp.get("templates", []) if t not in tids]
        fails += [f"template {t} must not be sent" for t in exp.get("no_templates", []) if t in tids]
        if "template_order" in exp:
            a, b = exp["template_order"]
            if a not in tids or b not in tids or tids.index(a) > tids.index(b):
                fails.append(f"{a} must come before {b} (sent: {tids})")
        if "photo_key" in exp:
            src = self.photo_src(exp["photo_key"])
            if not any(src and src in p.photos for p in self.sent):
                fails.append(f"photo {exp['photo_key']} not sent")
        if "admin_card" in exp and ("Запит #" in admin) != exp["admin_card"]:
            fails.append(f"admin card expected={exp['admin_card']}")
        fails += [f"admin group missing /{rx}/" for rx in exp.get("admin_contains", []) if not re.search(rx, admin, re.I)]
        handoffs = await self.st.pool.fetch("SELECT reason, department FROM handoffs WHERE chat_id = $1", self.chat)
        if "escalation" in exp and not any(h["reason"] in exp["escalation"] for h in handoffs):
            fails.append(f"no escalation {exp['escalation']} (got {[h['reason'] for h in handoffs]})")
        if "department" in exp and not any(h["department"] in exp["department"] for h in handoffs):
            fails.append(f"department not in {exp['department']} (got {[h['department'] for h in handoffs]})")
        if "order" in exp:
            fails += await self.check_order(exp["order"])
        if "payments" in exp and tids.count("payment_details") != exp["payments"]:
            fails.append(f"payment_details sent {tids.count('payment_details')}× (expected {exp['payments']})")
        if "mode" in exp and await self.st.get_mode(self.chat) != exp["mode"]:
            fails.append(f"mode {await self.st.get_mode(self.chat)} != {exp['mode']}")
        stats = self.session().stats if self.session() else SessionStats()
        if "replies" in exp and stats.replies != exp["replies"]:
            fails.append(f"reply batches {stats.replies} != {exp['replies']}")
        if "cancels_min" in exp and stats.cancels < exp["cancels_min"]:
            fails.append(f"cancelled generations {stats.cancels} < {exp['cancels_min']}")
        calls = await self.calls()
        if "llm_calls_max" in exp and len(calls) > exp["llm_calls_max"]:
            fails.append(f"{len(calls)} LLM calls > {exp['llm_calls_max']}")
        if "llm_delta" in exp and len(calls) - calls_before != exp["llm_delta"]:
            fails.append(f"{len(calls) - calls_before} LLM calls this turn != {exp['llm_delta']}")
        if exp.get("copied_to_client") and not any(c[0] == "copy" and c[1] == self.chat for c in self.tr.calls[start:]):
            fails.append("manager reply not copied to the client")
        copies = sum(c[0] == "copy" and c[1] == ADMIN for c in self.tr.calls[start:])
        if "admin_copies_min" in exp and copies < exp["admin_copies_min"]:
            fails.append("client message not copied to the admin group")
        if "assistant_replies" in exp:
            n = await self.st.pool.fetchval(
                "SELECT count(*) FROM messages WHERE chat_id = $1 AND role = 'assistant' AND meta IS NOT NULL", self.chat)
            if n != exp["assistant_replies"]:
                fails.append(f"{n} assistant replies in history != {exp['assistant_replies']} (orphan turn?)")
        return fails

    async def check_order(self, want: Any) -> list[str]:
        o = await self.st.pool.fetchrow("SELECT * FROM orders WHERE chat_id = $1 ORDER BY id DESC LIMIT 1", self.chat)
        if want == "none":
            return [f"no order expected, got {o['status']}"] if o else []
        if not o:
            return ["order expected, none created"]
        fails, d = [], o["data"]
        if "status" in want and o["status"] != want["status"]:
            fails.append(f"order status {o['status']} != {want['status']}")
        if "total" in want and d["totals"]["with_vat"] != want["total"]:
            fails.append(f"order total {d['totals']['with_vat']} != {want['total']}")
        if "buyer" in want and d["customer"]["buyer_type"] != want["buyer"]:
            fails.append(f"buyer {d['customer']['buyer_type']} != {want['buyer']}")
        for w in want.get("items", []):
            if not any(i["slug"] == w["slug"] and i["option_ids"] == w.get("options", []) and
                       i.get("color_id") == w.get("color") and i["qty"] == w.get("qty", 1) for i in d["items"]):
                fails.append(f"order item {w} missing (got {d['items']})")
        return fails

    async def common(self, case: dict) -> list[str]:
        """Checks every case gets: Ukrainian-only model text, catalog prices only, VAT pairing, no stray payment
        details, and at most one rejected attempt per accepted reply."""
        fails = []
        free = [p.text for p in self.sent if p.template_id is None and p.kind == "text"]
        for t in free:
            errs = self.val.language_errors(t)
            if errs:
                fails.append(f"language: {errs} in «{t[:80]}»")
        client_said = " ".join(t.get("say", "") for t in case["turns"])
        products = self.cat.products.values()
        allowed = {str(p["price"][k]) for p in products for k in ("with_vat", "without_vat")}
        allowed |= {str(o["price"][k]) for p in products for o in p["options"] for k in ("with_vat", "without_vat")}
        allowed |= numbers_in(client_said)
        for t in free:
            for m in PRICE_RE.findall(t):
                if re.sub(r"\D", "", m) not in allowed:
                    fails.append(f"price {m} грн in model text is not a catalog price")
        without = self.cat.without_vat_numbers(list(self.cat.products))
        for p in self.sent:
            if p.kind == "text" and {re.sub(r"\D", "", m) for m in PRICE_RE.findall(p.text)} & without \
                    and "сертифікат" not in p.text.lower():
                fails.append(f"without-VAT price without «сертифікат»: «{p.text[:80]}»")
        expects_pay = (case.get("expect") or {}).get("payments", 0) or any(
            (t.get("expect") or {}).get("payments", 0) for t in case["turns"])
        if not expects_pay and any(IBAN_RE.search(p.text) for p in self.sent):
            fails.append("payment details sent but not expected")
        calls = await self.calls()
        bad, ok = sum(c["status"] != "ok" for c in calls), sum(c["status"] == "ok" for c in calls)
        if bad > ok:
            fails.append(f"too many rejected attempts: {bad} rejected vs {ok} accepted")
        return fails


async def play(w: World, case: dict) -> list[str]:
    fails: list[str] = []
    for turn in case["turns"]:
        start, before = len(w.tr.calls), len(await w.calls())
        if "say" in turn:
            if "delay_ms" in turn:
                await asyncio.sleep(turn["delay_ms"] / 1000)
            elif turn.get("wait_for") == "generating":
                await w.generating()
            else:
                await w.idle()
                start, before = len(w.tr.calls), len(await w.calls())
            await w.say(turn["say"])
        else:
            await w.idle()
            start, before = len(w.tr.calls), len(await w.calls())
            await w.admin(turn["admin"])
        if "expect" in turn:
            await w.idle()
            label = turn.get("say", turn.get("admin"))[:30]
            fails += [f"[{label}] {f}" for f in await w.check(turn["expect"], start, before)]
    await w.idle()
    if case.get("expect"):
        fails += await w.check(case["expect"], 0, 0)
    return fails + await w.common(case)


@pytest.mark.parametrize("case,run", [(c, r) for c in CASES if not SELECT or c["id"] in SELECT for r in range(1, RUNS + 1)],
                         ids=lambda v: v["id"] if isinstance(v, dict) else f"run{v}")
async def test_case(case, run):
    if SPENT["usd"] >= BUDGET:
        pytest.skip(f"budget ${BUDGET} reached (${SPENT['usd']:.2f} spent)")
    w = World()
    await w.start()
    try:
        fails = await play(w, case)
        calls = await w.calls()
        spent = sum(llm_call_cost(r, w.settings.cache_ttl) for r in calls)
        SPENT["usd"] += spent
        SPENT["calls"] += len(calls)
        print(f"\n{case['id']} run{run} {'PASS' if not fails else 'FAIL'} · {len(calls)} calls · ${spent:.3f} · "
              f"total ${SPENT['usd']:.2f}")
        if fails:
            for c in w.tr.calls:
                if c[0] in ("text", "photos"):
                    who = "🤖" if c[1] == w.chat else "📋"
                    body = c[2] if c[0] == "text" else f"[photos] {c[3] or ''}"
                    print(f"      {who} " + body.replace("\n", " / ")[:220])
            for f in fails:
                print(f"   ✗ {f}")
        RESULTS.setdefault(case["id"], []).append((not fails, fails))
    finally:
        await w.close()
    assert not fails, "; ".join(fails)


def test_zz_summary():
    """Prints the per-case result table (runs last)."""
    print("\n\n══════ live harness summary ══════")
    for cid, runs in RESULTS.items():
        ok = sum(r[0] for r in runs)
        print(f"  {cid}: {ok}/{len(runs)} {'✅' if ok == len(runs) else '❌'}")
    print(f"  total: {SPENT['calls']} LLM calls · ≈ ${SPENT['usd']:.2f} (budget ${BUDGET})")
