"""From a batch of client messages to a checked reply: retrieval → prompt → model → checks (one retry) → templates."""
from __future__ import annotations

import html
import json
import logging
import time
from typing import Any

from .catalog import Catalog
from .config import Settings
from .handoff import REASON_UK, Handoff
from .llm import LLM, llm_call_cost
from .orders import EDITABLE, OrderPlan, Orders, order_template_issues
from .photos import PhotoCache
from .sessions import Incoming, Reply
from .storage import Storage
from .templates import ORDER_TEMPLATES, RenderedPart, Templates
from .transport import Transport
from .validation import COURTESY_ONLY_RE, PROMISE_RE, SUMMARY_TALK_RE, Check, Validator, numbers_in

log = logging.getLogger("salesbot")

BUDGET_ALERT_EVERY = 6 * 3600  # seconds between "daily AI budget reached" notices to the staff group


class LlmPipeline:
    def __init__(self, settings: Settings, storage: Storage, transport: Transport, catalog: Catalog,
                 templates: Templates, validator: Validator, llm: LLM, orders: Orders, handoff: Handoff,
                 photos: PhotoCache):
        self.settings, self.storage, self.transport = settings, storage, transport
        self.catalog, self.templates, self.validator, self.llm = catalog, templates, validator, llm
        self.orders, self.handoff, self.photos = orders, handoff, photos
        self._budget_alert_at: float | None = None  # monotonic time of the last budget notice

    async def is_human(self, chat_id: int) -> bool:
        return await self.handoff.is_human(chat_id)

    async def relay_to_human(self, chat_id: int, batch: list[Incoming]) -> None:
        for m in batch:
            await self.handoff.relay_client(chat_id, m.tg_id)
        await self.storage.mark_answered([m.db_id for m in batch if m.db_id])

    async def _orders(self, chat_id: int) -> tuple[dict | None, dict | None]:
        """(editable order, latest order) as plain dicts."""
        rows = await self.storage.pool.fetch("SELECT * FROM orders WHERE chat_id = $1 ORDER BY id DESC LIMIT 5", chat_id)
        current = next((dict(r) for r in rows if r["status"] in EDITABLE), None)
        return current, (dict(rows[0]) if rows else None)

    # ── usage limits ──
    async def over_limit(self, chat_id: int) -> str | None:
        """"user" or "budget" when this reply must not call the model (24 h windows); None otherwise."""
        s = self.settings
        if s.daily_replies_per_user and chat_id not in s.tester_ids \
                and await self.storage.replies_in_24h(chat_id) >= s.daily_replies_per_user:
            return "user"
        if s.daily_budget_usd:
            spent = sum(llm_call_cost(r, s.cache_ttl) for r in await self.storage.llm_calls_since(24))
            if spent >= s.daily_budget_usd:
                now = time.monotonic()
                if self._budget_alert_at is None or now - self._budget_alert_at > BUDGET_ALERT_EVERY:
                    self._budget_alert_at = now
                    log.warning("daily AI budget reached: $%.2f of $%.2f", spent, s.daily_budget_usd)
                    await self.handoff.notify(f"⚠️ Денний бюджет на ШІ вичерпано (≈ ${spent:.2f} з "
                                              f"${s.daily_budget_usd:.2f}). Бот відповідає шаблоном до кінця доби.")
                return "budget"
        return None

    async def limited_reply(self, chat_id: int, why: str) -> Reply:
        """The limit message once; after that the bot stays quiet until the window frees up."""
        part = self.templates.text("limit_reached")
        last = [r for r in await self.storage.history(chat_id, limit=6) if r["role"] != "user"]
        if last and last[-1]["text"] == part.history_text:
            return Reply([], {"limited": why})
        return Reply([part], {"limited": why})

    # ── generation ──
    async def generate(self, chat_id: int, batch: list[Incoming]) -> Reply:
        limit = await self.over_limit(chat_id)
        if limit:
            return await self.limited_reply(chat_id, limit)
        hist = await self.storage.history(chat_id, limit=self.settings.history_turns * 2,
                                          exclude_ids=[m.db_id for m in batch if m.db_id])
        with_meta = [r for r in hist if r["role"] == "assistant" and r["meta"]]
        recent = [s for r in with_meta[-6:] for s in r["meta"].get("products", [])]
        last_products = with_meta[-1]["meta"].get("products", []) if with_meta else []
        last = [s for s in last_products if s in self.catalog.products][:2]
        batch_text = "\n".join(m.text for m in batch)
        # Follow-ups ("а гарантія на нього?") refer to the last reply's products: they keep their card slots.
        slugs = (last + [s for s in self.catalog.retrieve(batch_text, recent=recent) if s not in last])[:3]
        cards = [self.catalog.card(s) for s in slugs]
        cards_json = "\n".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in cards)
        client = await self.storage.get_client(chat_id)
        flags = {"first_contact": not any(r["role"] == "assistant" for r in hist),
                 "client_name": client["full_name"] if client else None}

        current, latest = await self._orders(chat_id)
        order_state = self.orders.prompt_state(current, latest)
        state_json = order_state if isinstance(order_state, str) else json.dumps(order_state, ensure_ascii=False)

        allowed = (self.llm.reference_numbers | numbers_in(cards_json) | numbers_in(batch_text)
                   | numbers_in("\n".join(r["text"] for r in hist)) | numbers_in(state_json) | {"1"})
        for s in slugs:  # base price + any single option is a total the model may state
            p = self.catalog.products[s]
            allowed |= {str(p["price"]["with_vat"] + o["price"]["with_vat"]) for o in p["options"]}

        cards_text = cards_json or "none matched: rely on the catalog index, ask or say it is not in the catalog"
        messages: list[dict[str, Any]] = [*self._history_messages(hist), {"role": "user", "content": (
            f"<catalog_cards>\n{cards_text}\n</catalog_cards>\n<order_state>{state_json}</order_state>\n"
            f"<conversation_flags>{json.dumps(flags, ensure_ascii=False)}</conversation_flags>\n"
            f"{self._client_block(batch_text)}")}]

        for attempt in (1, 2):
            resp, call, latency = await self.llm.call(chat_id, messages, attempt)
            data = call["input"] if call else None
            discussed = set(last) | self.catalog.named_in(batch_text)
            check = (self.validator.check(data, allowed, self.catalog.without_vat_numbers(slugs), discussed) if data
                     else Check(["no `reply` tool call"]))
            plan = OrderPlan()
            if data and not check.errors:  # order changes are checked like any other part of the reply
                plan = self.orders.plan(current, latest, data["order_update"])
                check.errors += [f"order_update: {e}" for e in plan.errors]
                if not plan.errors:
                    sent = [p["template_id"] for p in data["parts"]
                            if p.get("kind") == "template" and p.get("template_id") in ORDER_TEMPLATES]
                    missing = self.orders.missing(plan.data) if plan.data else None
                    issues = order_template_issues(sent, plan, missing)
                    if issues and attempt == 1:
                        check.errors += issues
                    elif issues:  # second attempt: correct it rather than fail (drop not-due, append due ones)
                        log.warning("chat %s: order templates auto-corrected: %s", chat_id, issues)
                    if not data["parts"] and not plan.parts:
                        check.errors.append("parts is empty but nothing is due this turn (the order is not complete;"
                                            f" missing: {missing}). Write the reply yourself.")
            await self.storage.log_llm_call(chat_id, self.settings.model, attempt, resp.usage, latency,
                                            "ok" if not check.errors else "invalid",
                                            {"errors": check.errors, "dropped": sorted(check.drop_parts),
                                             "cards": slugs, "intent": data and data.get("intent"),
                                             "parts": data.get("parts") if data and check.errors else None})
            if data and not check.errors:
                return self._build(data, check, plan)
            log.warning("chat %s attempt %d rejected: %s", chat_id, attempt, check.errors)
            if attempt == 1 and call:
                # The assistant turn goes back unchanged (thinking blocks included: newer models reject edited ones).
                messages = [
                    *messages,
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                                                  "content": "Rejected, nothing was sent. Fix these problems and call "
                                                             "`reply` again:\n- " + "\n- ".join(check.errors)}]},
                ]
            elif attempt == 1 and resp.content and resp.stop_reason == "end_turn":
                # tool_choice "auto" models can answer in plain text: ask once more for the tool call.
                messages = [
                    *messages,
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": "Nothing was sent to the client. Answer by calling the `reply` tool."},
                ]
        part = self.templates.text("fallback_handoff")
        part.meta = {"products": [], "escalate": {"needed": True, "reason": "validator_failed"}}
        summary = f"Бот не зміг відповісти на: «{batch_text[:300]}»"
        return Reply([part], {"escalate": {"needed": True, "reason": "validator_failed", "department": "sales",
                                           "summary_uk": summary}, "model_text": True})

    @staticmethod
    def _client_block(text: str) -> str:
        safe = text.replace("<", "‹").replace(">", "›")  # the client can't close or forge our tags
        return f'<client_messages untrusted="true">\n{safe}\n</client_messages>'

    def _history_messages(self, rows: list) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
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

    def _build(self, data: dict, check: Check, plan: OrderPlan) -> Reply:
        due = {p.template_id: p for p in plan.parts}  # order templates the order state makes due, values filled
        parts: list[RenderedPart] = []
        used: set[str | None] = set()
        for i, part in enumerate(data["parts"]):
            if i in check.drop_parts:
                continue
            if part["kind"] == "text":
                text = part["text"].strip()
                parts.append(RenderedPart("text", html.escape(text), history_text=text))
            elif part["template_id"] in ORDER_TEMPLATES:
                tid = part["template_id"]
                if tid in due and tid not in used:  # where the model put it, with Python's values
                    parts.append(due[tid])
                    used.add(tid)
                # not due (only possible after the second attempt): dropped, never sent
            else:
                parts.append(self.templates.render(part))
        parts += [p for tid, p in due.items() if tid not in used]  # due but forgotten (second attempt): appended
        escalate = data["escalate"]
        if check.force_escalate:
            escalate = {**escalate, "needed": True, "reason": check.force_escalate}

        def free(p: RenderedPart) -> bool:  # the model's own words
            return p.kind == "text" and p.template_id is None

        if plan.status in ("PAYMENT_SENT", "MANAGER_REVIEW"):
            # A confirm turn: the order templates say everything; free text could promise the wrong outcome.
            parts = [p for p in parts if not free(p)]
        elif plan.status == "AWAITING_CONFIRMATION" and due and plan.data:
            # The summary goes out: drop model text that repeats it or is a bare courtesy line («Дякую!»),
            # keep anything else it answered.
            total = str(plan.data["totals"]["with_vat"])
            parts = [p for p in parts if not free(p) or not (
                total in numbers_in(p.history_text) or SUMMARY_TALK_RE.search(p.history_text)
                or COURTESY_ONLY_RE.fullmatch(p.history_text.strip()))]
        if not parts:  # validated against, but never send an empty reply
            parts = [self.templates.text("fallback_handoff")]
        parts[0].meta = {"products": [s for s in data["products_referenced"] if s in self.catalog.products],
                         "intent": data["intent"], "escalate": escalate, "order_update": data["order_update"]}
        model_text = any(p["kind"] == "text" for i, p in enumerate(data["parts"]) if i not in check.drop_parts)
        has_dept = any(p.get("template_id") == "department_contact" for p in data["parts"])
        return Reply(parts, {"plan": plan, "escalate": escalate, "model_text": model_text, "has_dept": has_dept})

    async def send_part(self, chat_id: int, part: RenderedPart) -> None:
        if part.kind == "photos" and await self.photos.send(self.transport, chat_id, part.photos, part.text):
            return
        await self.transport.send_text(chat_id, part.text)  # text, or the caption when no photo could be sent

    async def commit(self, chat_id: int, batch: list[Incoming], sent_parts: list[RenderedPart], reply: Reply) -> None:
        """After delivery: history, then the order write and any staff-group action (never for a cancelled reply)."""
        if sent_parts and reply.extra.get("first_part_after_s") is not None:  # kept for /stats (latency p50/p90)
            sent_parts[0].meta = {**(sent_parts[0].meta or {}), "first_part_after_s": reply.extra["first_part_after_s"]}
        for part in sent_parts:
            await self.storage.save_message(chat_id, "assistant", part.history_text, meta=part.meta)
        await self.storage.mark_answered([m.db_id for m in batch if m.db_id])

        plan: OrderPlan | None = reply.extra.get("plan")
        if plan and plan.status:
            if plan.order_id:
                await self.storage.pool.execute(
                    "UPDATE orders SET status = $2, data = $3, updated_at = now(),"
                    " payment_sent_at = CASE WHEN $4 THEN coalesce(payment_sent_at, now()) ELSE payment_sent_at END"
                    " WHERE id = $1", plan.order_id, plan.status, plan.data, plan.payment_sent)
                order_id = plan.order_id
            else:
                order_id = await self.storage.pool.fetchval(
                    "INSERT INTO orders (chat_id, status, data) VALUES ($1, $2, $3) RETURNING id",
                    chat_id, plan.status, plan.data)
            log.info("order %s → %s (chat %s)", order_id, plan.status, chat_id)
            if plan.notify:
                await self.handoff.notify_order(plan.notify, order_id, chat_id)
            if plan.review_reason:
                await self.handoff.open(chat_id, plan.review_reason, "sales",
                                        f"Замовлення №{order_id} підтверджено, потрібне оформлення "
                                        f"({REASON_UK[plan.review_reason]}).", human=True)

        esc = reply.extra.get("escalate") or {}
        if esc.get("needed"):
            human = not reply.extra.get("has_dept")  # pure department routing: the bot keeps the conversation
            await self.handoff.open(chat_id, esc.get("reason", "unknown_fact"), esc.get("department"),
                                    esc.get("summary_uk"), human)
            promised = any(PROMISE_RE.search(p.history_text) for p in sent_parts)  # e.g. vat_explainer's «я уточню»
            if human and not promised:  # nothing sent tells the client someone follows up: acknowledge once
                ack = self.templates.text("handoff_ack")
                await self.send_part(chat_id, ack)
                await self.storage.save_message(chat_id, "assistant", ack.history_text)

    async def on_error(self, chat_id: int, batch: list[Incoming], exc: Exception) -> None:
        part = self.templates.text("fallback_handoff")
        await self.send_part(chat_id, part)
        text = " / ".join(m.text for m in batch)[:300]
        await self.commit(chat_id, batch, [part], Reply([part], {"model_text": True, "escalate": {
            "needed": True, "reason": "validator_failed", "department": "sales",
            "summary_uk": f"Помилка бота ({type(exc).__name__}) на: «{text}»"}}))
