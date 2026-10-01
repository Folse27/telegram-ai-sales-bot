"""Fake-client harness (PLAN.md §10): the real pipeline and the real model (MODEL from .env), with a fake Telegram,
a fake admin group and a throwaway schema in the test database. Cases live in tests/cases.yaml.

It spends API money, so it only runs when asked:
    CMD:       set RUN_LIVE=1 && python -m pytest tests/test_live.py -q -s
    Git Bash:  RUN_LIVE=1 python -m pytest tests/test_live.py -q -s
Options: LIVE_RUNS=3 (runs per case; a case passes only if all pass) · LIVE_CASES=TC01,TC09 (subset) ·
         LIVE_BUDGET=4 (USD; remaining runs are skipped once the total passes it).
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import FakeTransport

import main
from config import get_settings

CASES = yaml.safe_load((Path(__file__).parent / "cases.yaml").read_text(encoding="utf-8"))["cases"]
RUNS = int(os.environ.get("LIVE_RUNS", "3"))
BUDGET = float(os.environ.get("LIVE_BUDGET", "4"))
SELECT = {c.strip() for c in os.environ.get("LIVE_CASES", "").split(",") if c.strip()}
ADMIN = -100999
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0)}
SPENT = {"usd": 0.0, "calls": 0}
RESULTS: dict[str, list[tuple[bool, list[str]]]] = {}
IBAN_RE = re.compile(r"UA\d{27}")
PRICE_RE = re.compile(r"(\d{1,3}(?:[  ]\d{3})+|\d+)\s*грн", re.I)

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1",
                                reason="live harness: set RUN_LIVE=1 (calls the real model, costs money)")


def cost(rows, model: str, ttl: str) -> float:
    p_in, p_out = PRICES.get(model, (2.0, 10.0))
    write = p_in * (2.0 if ttl == "1h" else 1.25)
    return sum(r["input_tokens"] * p_in + r["output_tokens"] * p_out + (r["cache_read_tokens"] or 0) * p_in * 0.1
               + (r["cache_write_tokens"] or 0) * write for r in rows) / 1e6


class World:
    """One chat against the real pipeline; records everything the bot sends."""

    chat = 555

    async def start(self):
        s = get_settings().model_copy(update={"debounce_seconds": 0.3, "debounce_max_wait": 2.0,
                                              "part_gap_min": 0.0, "part_gap_max": 0.0})
        self.settings = s
        self.schema = f"t_{uuid.uuid4().hex[:10]}"
        self.st = await main.Storage.connect(s.test_database_url, schema=self.schema)
        self.tr = FakeTransport()
        self.cat, content = main.Catalog.load(), main.Templates.load_content()
        tpl = main.Templates(content, self.cat, s.persona_name)
        self.val = main.Validator(self.cat, content, s.max_parts)
        orders = main.Orders(self.cat, tpl)
        self.ho = main.Handoff(SimpleNamespace(admin_chat_id=ADMIN), self.st, self.tr, tpl, orders)
        self.pl = main.LlmPipeline(s, self.st, self.tr, self.cat, tpl, self.val, main.LLM(s, self.cat, content, self.st),
                                   orders, self.ho)
        self.sent: list[RenderedLike] = []
        real_send = self.pl.send_part

        async def spy(chat_id, part):  # which template each client message came from
            self.sent.append(SimpleNamespace(template_id=part.template_id, kind=part.kind, text=part.text,
                                             file_ids=list(part.file_ids)))
            await real_send(chat_id, part)

        self.pl.send_part = spy
        self.sm = main.SessionManager(self.pl, self.tr, main.SessionConfig.from_settings(s))

        async def resume(chat_id):
            for r in await self.ho.trailing_client_messages(chat_id):
                await self.sm.on_message(main.Incoming(chat_id, r["text"], r["id"], r["tg_message_id"] or 0))

        self.ho.on_resume = resume
        self.tg = 0
        await self.st.upsert_client(self.chat, self.chat, "tester", "Тест")

    async def close(self):
        await self.st.pool.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.st.close()

    async def say(self, text: str):
        self.tg += 1
        db = await self.st.save_message(self.chat, "user", text, self.tg, answered=False)
        await self.sm.on_message(main.Incoming(self.chat, text, db, self.tg))

    def session(self):
        return self.sm.sessions.get(self.chat)

    async def idle(self, timeout: float = 90):
        await asyncio.sleep(0.05)
        for _ in range(int(timeout / 0.05)):
            s = self.session()
            if s is None or (s.state == main.IDLE and not s.buffer):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("session did not become idle")

    async def generating(self, timeout: float = 20):
        for _ in range(int(timeout / 0.02)):
            s = self.session()
            if s and s.state == main.GENERATING:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("session never started generating")

    async def admin(self, action: str):
        h = await self.st.pool.fetchrow("SELECT id, card_message_id FROM handoffs WHERE chat_id = $1 ORDER BY id DESC LIMIT 1",
                                        self.chat)
        if action == "paid":
            oid = await self.st.pool.fetchval("SELECT max(id) FROM orders WHERE chat_id = $1", self.chat)
            await self.ho.command(f"/paid {oid}")
        elif action in ("take", "bot", "close"):
            await self.ho.button(h["id"], action, "Тестовий менеджер")
        elif action.startswith("reply:"):
            await self.ho.manager_reply(h["card_message_id"], 990000 + self.tg, action.removeprefix("reply:"))

    async def calls(self):
        return await self.st.pool.fetch("SELECT * FROM llm_calls ORDER BY id")

    # ── checks ──
    async def check(self, exp: dict, start: int, calls_before: int) -> list[str]:
        fails: list[str] = []
        client = [c for c in self.tr.calls[start:] if c[0] in ("text", "photo", "media_group") and c[1] == self.chat]
        text = "\n".join(c[2] if c[0] == "text" else (c[3] or "") for c in client)
        admin = "\n".join(c[2] for c in self.tr.calls[start:] if c[0] == "text" and c[1] == ADMIN)
        sent = self.sent  # template ids of everything sent (whole case; order matters for template_order)
        free = "\n".join(p.text for p in sent if p.template_id is None and p.kind == "text")
        tids = [p.template_id for p in sent if p.template_id]
        for rx in exp.get("contains", []):
            if not re.search(rx, text, re.I):
                fails.append(f"missing /{rx}/")
        for rx in exp.get("not_contains", []):
            if re.search(rx, text, re.I):
                fails.append(f"forbidden /{rx}/ found")
        for rx in exp.get("free_not_contains", []):
            if re.search(rx, free, re.I):
                fails.append(f"model text has forbidden /{rx}/")
        for t in exp.get("templates", []):
            if t not in tids:
                fails.append(f"template {t} not sent (sent: {tids})")
        for t in exp.get("no_templates", []):
            if t in tids:
                fails.append(f"template {t} must not be sent")
        if "template_order" in exp:
            a, b = exp["template_order"]
            if a not in tids or b not in tids or tids.index(a) > tids.index(b):
                fails.append(f"{a} must come before {b} (sent: {tids})")
        if "photo_key" in exp:
            fid = self.cat.file_ids[exp["photo_key"]]
            if not any(fid in c[2] if isinstance(c[2], list) else c[2] == fid for c in client if c[0] != "text"):
                fails.append(f"photo {exp['photo_key']} not sent")
        if "admin_card" in exp and ("Запит #" in admin) != exp["admin_card"]:
            fails.append(f"admin card expected={exp['admin_card']}")
        for rx in exp.get("admin_contains", []):
            if not re.search(rx, admin, re.I):
                fails.append(f"admin group missing /{rx}/")
        handoffs = await self.st.pool.fetch("SELECT reason, department FROM handoffs WHERE chat_id = $1", self.chat)
        if "escalation" in exp and not any(h["reason"] in exp["escalation"] for h in handoffs):
            fails.append(f"no escalation {exp['escalation']} (got {[h['reason'] for h in handoffs]})")
        if "department" in exp and not any(h["department"] in exp["department"] for h in handoffs):
            fails.append(f"department not in {exp['department']} (got {[h['department'] for h in handoffs]})")
        if "order" in exp:
            o = await self.st.pool.fetchrow("SELECT * FROM orders WHERE chat_id = $1 ORDER BY id DESC LIMIT 1", self.chat)
            want = exp["order"]
            if want == "none":
                if o:
                    fails.append(f"no order expected, got {o['status']}")
            elif not o:
                fails.append("order expected, none created")
            else:
                d = o["data"]
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
        if "payments" in exp:
            n = tids.count("payment_details")
            if n != exp["payments"]:
                fails.append(f"payment_details sent {n}× (expected {exp['payments']})")
        if "mode" in exp and await self.st.get_mode(self.chat) != exp["mode"]:
            fails.append(f"mode {await self.st.get_mode(self.chat)} != {exp['mode']}")
        stats = self.session().stats if self.session() else main.SessionStats()
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
        if "admin_copies_min" in exp and sum(c[0] == "copy" and c[1] == ADMIN for c in self.tr.calls[start:]) < exp["admin_copies_min"]:
            fails.append("client message not copied to the admin group")
        if "assistant_replies" in exp:
            n = await self.st.pool.fetchval(
                "SELECT count(*) FROM messages WHERE chat_id = $1 AND role = 'assistant' AND meta IS NOT NULL", self.chat)
            if n != exp["assistant_replies"]:
                fails.append(f"{n} assistant replies in history != {exp['assistant_replies']} (orphan turn?)")
        return fails

    async def common(self, case: dict) -> list[str]:
        fails = []
        free = [p.text for p in self.sent if p.template_id is None and p.kind == "text"]
        for t in free:
            errs = self.val.language_errors(t)
            if errs:
                fails.append(f"language: {errs} in «{t[:80]}»")
        client_said = " ".join(t.get("say", "") for t in case["turns"])
        allowed = {str(p["price"][k]) for p in self.cat.products.values() for k in ("with_vat", "without_vat")}
        allowed |= {str(o["price"][k]) for p in self.cat.products.values() for o in p["options"] for k in ("with_vat", "without_vat")}
        allowed |= main.numbers_in(client_said)
        for t in free:
            for m in PRICE_RE.findall(t):
                if re.sub(r"\D", "", m) not in allowed:
                    fails.append(f"price {m} грн in model text is not a catalog price")
        without = self.cat.without_vat_numbers(list(self.cat.products))
        for p in self.sent:
            if p.kind == "text" and {re.sub(r"\D", "", m) for m in PRICE_RE.findall(p.text)} & without \
                    and "сертифікат" not in p.text.lower():
                fails.append(f"без-ПДВ price without «сертифікат»: «{p.text[:80]}»")
        expects_pay = (case.get("expect") or {}).get("payments", 0) or any(
            (t.get("expect") or {}).get("payments", 0) for t in case["turns"])
        if not expects_pay and any(IBAN_RE.search(p.text) for p in self.sent):
            fails.append("payment details sent but not expected")
        calls = await self.calls()
        bad, ok = sum(c["status"] != "ok" for c in calls), sum(c["status"] == "ok" for c in calls)
        if bad > ok:
            fails.append(f"too many rejected attempts: {bad} rejected vs {ok} accepted")
        return fails


@pytest.mark.parametrize("case,run", [(c, r) for c in CASES if not SELECT or c["id"] in SELECT for r in range(1, RUNS + 1)],
                         ids=lambda v: v["id"] if isinstance(v, dict) else f"run{v}")
async def test_case(case, run):
    if SPENT["usd"] >= BUDGET:
        pytest.skip(f"budget ${BUDGET} reached (${SPENT['usd']:.2f} spent)")
    w = World()
    await w.start()
    fails: list[str] = []
    try:
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
                fails += [f"[{turn.get('say', turn.get('admin'))[:30]}] {f}" for f in await w.check(turn["expect"], start, before)]
        await w.idle()
        if case.get("expect"):
            fails += await w.check(case["expect"], 0, 0)
        fails += await w.common(case)
        calls = await w.calls()
        spent = cost(calls, w.settings.model, w.settings.cache_ttl)
        SPENT["usd"] += spent
        SPENT["calls"] += len(calls)
        transcript = "\n".join(
            f"      {'🤖' if c[1] == w.chat else '📋'} " + (c[2] if c[0] == "text" else f"[{c[0]}] {c[3] or ''}").replace("\n", " / ")[:220]
            for c in w.tr.calls if c[0] in ("text", "photo", "media_group"))
        print(f"\n{case['id']} run{run} {'PASS' if not fails else 'FAIL'} · {len(calls)} calls · ${spent:.3f} · total ${SPENT['usd']:.2f}")
        if fails:
            print(transcript)
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


RenderedLike = SimpleNamespace
