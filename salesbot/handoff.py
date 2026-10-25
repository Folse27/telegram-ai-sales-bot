"""Handover to people: a card with buttons in the staff group, a two-way relay, and the staff commands."""
from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .catalog import fmt_uah
from .llm import llm_call_cost
from .orders import FINAL, Orders
from .storage import Storage
from .templates import RenderedPart, Templates
from .transport import Transport

log = logging.getLogger("salesbot")

REASON_UK = {
    "unknown_fact": "питання поза каталогом", "vat_eligibility": "ціна без ПДВ",
    "legal_entity": "замовлення від юрособи / в/ч", "bulk_or_custom": "нестандартний запит", "complaint": "скарга",
    "payment_issue": "оплата / реквізити", "client_request": "клієнт просить людину", "abuse": "некоректна поведінка",
    "validator_failed": "бот не зміг відповісти",
}
HUMAN_EXPIRES_SQL = "interval '12 hours'"  # a chat a person took over returns to the bot after 12 h of silence


def handoff_keyboard(handoff_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✋ Взяти", callback_data=f"ho:take:{handoff_id}"),
        InlineKeyboardButton(text="🤖 Повернути боту", callback_data=f"ho:bot:{handoff_id}"),
        InlineKeyboardButton(text="✅ Закрити", callback_data=f"ho:close:{handoff_id}"),
    ]])


def percentile(xs: list[float], q: float) -> float:
    return xs[min(len(xs) - 1, int(q * len(xs)))]


class Handoff:
    def __init__(self, settings: Any, storage: Storage, transport: Transport, templates: Templates, orders: Orders):
        self.settings, self.storage, self.transport = settings, storage, transport
        self.templates, self.orders = templates, orders
        self.admin: int = settings.admin_chat_id
        self.departments = templates.content["departments"]
        # Set by the runtime: answers the client's trailing messages when a manager hands the chat back.
        self.on_resume: Callable[[int], Awaitable[None]] | None = None

    async def is_human(self, chat_id: int) -> bool:
        """HUMAN mode, with the lazy 12 h auto-return (no timers, so the database can sleep)."""
        row = await self.storage.pool.fetchrow(
            "SELECT mode, greatest(mode_changed_at, coalesce(last_manager_at, mode_changed_at))"
            f" < now() - {HUMAN_EXPIRES_SQL} AS expired FROM clients WHERE chat_id = $1", chat_id)
        if not row or row["mode"] != "HUMAN":
            return False
        if row["expired"]:
            await self.return_to_bot(chat_id, "expired")
            return False
        return True

    async def _open_handoff(self, chat_id: int) -> Any:
        return await self.storage.pool.fetchrow(
            "SELECT * FROM handoffs WHERE chat_id = $1 AND status IN ('open', 'taken') ORDER BY id DESC LIMIT 1", chat_id)

    async def open(self, chat_id: int, reason: str, department: str | None, summary: str | None, human: bool) -> None:
        """Post (or extend) the staff card; HUMAN mode unless it was only a department routing."""
        if human:
            await self.storage.set_mode(chat_id, "HUMAN")
        dept = self.departments.get(department or "", {}).get("title_uk", "—")
        existing = await self._open_handoff(chat_id)
        if existing and existing["card_message_id"]:
            mid = await self.transport.send_text(
                self.admin, f"➕ <b>Ще запит</b> ({html.escape(REASON_UK.get(reason, reason))}, {html.escape(dept)}): "
                            f"{html.escape(summary or '—')}", reply_to=existing["card_message_id"])
            await self._map(mid, chat_id, existing["id"])
            return
        hid = await self.storage.pool.fetchval(
            "INSERT INTO handoffs (chat_id, reason, department, summary) VALUES ($1, $2, $3, $4) RETURNING id",
            chat_id, reason, department, summary)
        mid = await self.transport.send_text(self.admin, await self._card(hid, chat_id, reason, dept, summary, human),
                                             reply_markup=handoff_keyboard(hid))
        await self.storage.pool.execute("UPDATE handoffs SET card_message_id = $2 WHERE id = $1", hid, mid)
        await self._map(mid, chat_id, hid)

    async def _card(self, hid: int, chat_id: int, reason: str, dept: str, summary: str | None, human: bool) -> str:
        c = await self.storage.get_client(chat_id)
        who = html.escape(c["full_name"] or "—") if c else "—"
        if c and c["username"]:
            who += f" (@{html.escape(c['username'])})"
        rows = await self.storage.history(chat_id, limit=5)
        icon = {"user": "👤", "assistant": "🤖", "manager": "🧑‍💼"}
        last = "\n".join(f"{icon[r['role']]} {html.escape(r['text'][:200])}" for r in rows) or "—"
        order = await self.order_snapshot(chat_id)
        mode = "бот мовчить, відповідайте реплаєм на це повідомлення" if human else "бот продовжує розмову"
        return (f"🔔 <b>Запит #{hid}</b> — {html.escape(REASON_UK.get(reason, reason))}\n"
                f"Клієнт: {who} · id <code>{chat_id}</code>\nВідділ: {html.escape(dept)}\n"
                f"Суть: {html.escape(summary or '—')}\n\n<b>Останні повідомлення:</b>\n{last}\n\n"
                f"<b>Замовлення:</b> {order}\n\n<i>Режим: {mode}.</i>")

    async def order_snapshot(self, chat_id: int) -> str:
        r = await self.storage.pool.fetchrow("SELECT * FROM orders WHERE chat_id = $1 ORDER BY id DESC LIMIT 1", chat_id)
        if not r:
            return "немає"
        d = r["data"]
        c = d["customer"]
        buyer = {"individual": "фізособа", "fop": "ФОП", "legal_entity": "юрособа", "military_unit": "в/ч"}.get(
            c.get("buyer_type"), "тип покупця не вказано")
        who = ", ".join(html.escape(str(c[k])) for k in ("full_name", "phone", "city") if c.get(k))
        np = f", НП {html.escape(str(c['np_branch']))}" if c.get("np_branch") else ""
        return (f"№{r['id']} {r['status']}, {fmt_uah(d['totals']['with_vat'])} грн\n{self.orders.lines(d)}"
                f"\n{who or 'контакти ще не надано'}{np} · {buyer}")

    async def _map(self, admin_message_id: int, chat_id: int, handoff_id: int | None) -> None:
        if admin_message_id:
            await self.storage.pool.execute(
                "INSERT INTO relay_map (admin_message_id, chat_id, handoff_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                admin_message_id, chat_id, handoff_id)

    async def relay_client(self, chat_id: int, tg_message_id: int) -> None:
        """HUMAN mode: copy the client's message into the staff group under the open card."""
        h = await self._open_handoff(chat_id)
        if not h:  # e.g. HUMAN set by /bot misuse: open a minimal card so the message isn't lost
            await self.open(chat_id, "client_request", None, "Повідомлення клієнта в режимі менеджера", human=True)
            h = await self._open_handoff(chat_id)
        mid = await self.transport.copy_message(self.admin, chat_id, tg_message_id, reply_to=h["card_message_id"])
        await self._map(mid, chat_id, h["id"])

    async def manager_reply(self, replied_to_id: int, admin_message_id: int, text: str) -> int | None:
        """A manager replied to a mapped message: copy it to the client. Returns the client chat id."""
        m = await self.storage.pool.fetchrow("SELECT * FROM relay_map WHERE admin_message_id = $1", replied_to_id)
        if not m:
            return None
        await self.transport.copy_message(m["chat_id"], self.admin, admin_message_id)
        await self.storage.save_message(m["chat_id"], "manager", text)
        await self.storage.pool.execute("UPDATE clients SET last_manager_at = now() WHERE chat_id = $1", m["chat_id"])
        await self.storage.pool.execute(
            "UPDATE messages SET answered = true WHERE chat_id = $1 AND role = 'user' AND NOT answered", m["chat_id"])
        return int(m["chat_id"])

    async def return_to_bot(self, chat_id: int, status: str = "returned", resume: bool = False) -> None:
        await self.storage.set_mode(chat_id, "BOT")
        await self.storage.pool.execute(
            "UPDATE handoffs SET status = $2, closed_at = now() WHERE chat_id = $1 AND status IN ('open', 'taken')",
            chat_id, status)
        if resume and self.on_resume:  # the client's last message(s) may still be unanswered
            await self.on_resume(chat_id)

    async def trailing_client_messages(self, chat_id: int) -> list:
        """User messages after the last bot or manager message: what the client is still waiting on."""
        return await self.storage.pool.fetch(
            "SELECT id, text, tg_message_id FROM messages WHERE chat_id = $1 AND role = 'user' AND id > coalesce("
            "(SELECT max(id) FROM messages WHERE chat_id = $1 AND role <> 'user'), 0) ORDER BY id", chat_id)

    async def button(self, handoff_id: int, action: str, admin_name: str) -> str:
        """Inline buttons on the card. Returns the toast text for the admin."""
        h = await self.storage.pool.fetchrow("SELECT * FROM handoffs WHERE id = $1", handoff_id)
        if not h:
            return "Запит не знайдено"
        if action == "take":
            await self.storage.pool.execute("UPDATE handoffs SET status = 'taken', taken_by = $2 WHERE id = $1",
                                            handoff_id, admin_name)
            await self.storage.set_mode(h["chat_id"], "HUMAN")
            mid = await self.transport.send_text(self.admin, f"✋ Запит #{handoff_id} взяв {html.escape(admin_name)}. "
                                                             "Відповідайте реплаєм — бот мовчить.",
                                                 reply_to=h["card_message_id"])
            await self._map(mid, h["chat_id"], handoff_id)  # a reply to this notice also reaches the client
            return "Взято. Бот мовчить."
        await self.transport.edit_markup(self.admin, h["card_message_id"], None)
        label = "повернуто боту" if action == "bot" else "закрито"
        mid = await self.transport.send_text(self.admin, f"🤖 Запит #{handoff_id} {label} ({html.escape(admin_name)}).",
                                             reply_to=h["card_message_id"])
        await self._map(mid, h["chat_id"], handoff_id)
        await self.return_to_bot(h["chat_id"], "returned" if action == "bot" else "closed", resume=True)
        return "Бот знову відповідає клієнту."

    # ── staff commands ──
    async def command(self, text: str) -> str:
        parts = text.split()
        cmd, arg = parts[0].split("@")[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "/stats":
            return await self.stats(int(arg) if arg.isdigit() else 24)
        if cmd == "/orders":
            rows = await self.storage.pool.fetch("SELECT * FROM orders ORDER BY id DESC LIMIT 10")
            return "\n".join(f"№{r['id']} · {r['status']} · {fmt_uah(r['data']['totals']['with_vat'])} грн · чат "
                             f"<code>{r['chat_id']}</code>" for r in rows) or "Замовлень ще немає."
        if not arg.isdigit():
            return "Формат: /paid 17 · /cancel 17 · /bot 123456789 · /orders · /stats [годин]"
        if cmd == "/bot":
            await self.return_to_bot(int(arg), "returned", resume=True)
            return f"🤖 Чат {arg}: бот знову відповідає."
        order = await self.storage.pool.fetchrow("SELECT * FROM orders WHERE id = $1", int(arg))
        if not order:
            return f"Замовлення №{arg} не знайдено."
        if cmd == "/paid":
            if order["status"] != "PAYMENT_SENT":
                return f"№{arg} має статус {order['status']}: /paid можливий лише після надсилання реквізитів."
            await self._set_order(order, "PAID")
            await self._tell_client(order["chat_id"], self.templates.text("payment_received"))
            return f"✅ №{arg}: оплату зафіксовано, клієнта повідомлено."
        if cmd == "/cancel":
            if order["status"] in FINAL:
                return f"№{arg} уже {order['status']}."
            await self._set_order(order, "CANCELLED")
            await self._tell_client(order["chat_id"], self.templates.text("order_cancelled", order_id=order["id"]))
            return f"❌ №{arg} скасовано, клієнта повідомлено."
        return "Невідома команда. Доступні: /paid, /cancel, /bot, /orders, /stats"

    async def stats(self, hours: int = 24) -> str:
        """/stats [hours]: replies, AI cost, cache share, latency, orders and open requests (0 = all time)."""
        since = "($1 = 0 OR created_at > now() - make_interval(hours => $1))"
        calls = await self.storage.llm_calls_since(hours)
        lat = sorted(float(r["meta"]["first_part_after_s"]) for r in await self.storage.pool.fetch(
            f"SELECT meta FROM messages WHERE role = 'assistant' AND meta ? 'first_part_after_s' AND {since}",
            hours))
        orders = await self.storage.pool.fetch(
            f"SELECT status, count(*) n FROM orders WHERE {since} GROUP BY status ORDER BY status", hours)
        open_h = await self.storage.pool.fetchval("SELECT count(*) FROM handoffs WHERE status IN ('open', 'taken')")
        ok = [r for r in calls if r["status"] == "ok"]
        usd = sum(llm_call_cost(r, getattr(self.settings, "cache_ttl", "1h")) for r in calls)
        reads = sum(r["cache_read_tokens"] or 0 for r in calls)
        cached_in = reads + sum((r["cache_write_tokens"] or 0) + (r["input_tokens"] or 0) for r in calls)
        llm_ms = sorted(float(r["latency_ms"]) for r in ok if r["latency_ms"])
        period = f"за {hours} год" if hours else "за весь час"
        lines = [
            f"📊 <b>Статистика {period}</b>",
            f"Відповідей: {len(ok)} · спроб відхилено валідатором: {len(calls) - len(ok)}",
            f"Вартість LLM ≈ ${usd:.2f}" + (f" (≈ ${usd / len(ok):.3f} за відповідь)" if ok else ""),
            f"Кеш: прочитано {reads / cached_in:.0%} вхідних токенів" if cached_in else "Кеш: даних ще немає",
            (f"LLM: p50 {percentile(llm_ms, .5) / 1000:.1f} с · p90 {percentile(llm_ms, .9) / 1000:.1f} с"
             if llm_ms else "LLM: даних ще немає"),
            (f"Від останнього повідомлення клієнта до першої відповіді: p50 {percentile(lat, .5):.1f} с · "
             f"p90 {percentile(lat, .9):.1f} с ({len(lat)} відп.)" if lat else "Час до першої відповіді: даних ще немає"),
            "Замовлення: " + (", ".join(f"{r['status']} {r['n']}" for r in orders) or "немає"),
            f"Відкритих запитів до менеджера: {open_h}",
        ]
        return "\n".join(lines)

    async def _set_order(self, order: Any, status: str) -> None:
        await self.storage.pool.execute("UPDATE orders SET status = $2, updated_at = now() WHERE id = $1",
                                        order["id"], status)

    async def _tell_client(self, chat_id: int, part: RenderedPart) -> None:
        await self.transport.send_text(chat_id, part.text)
        await self.storage.save_message(chat_id, "assistant", part.history_text)

    async def notify(self, text: str) -> None:
        """A plain notice to the staff group (e.g. the daily AI budget was reached)."""
        await self.transport.send_text(self.admin, text)

    async def notify_order(self, kind: str, order_id: int, chat_id: int) -> None:
        snap = await self.order_snapshot(chat_id)
        if kind == "payment_sent":
            text = (f"🧾 <b>Замовлення №{order_id}</b>: реквізити надіслано, очікуємо оплату.\n{snap}\n\n"
                    f"/paid {order_id} — оплату отримано · /cancel {order_id} — скасувати")
        else:
            text = f"❌ <b>Замовлення №{order_id}</b> скасовано клієнтом.\n{snap}"
        await self._map(await self.transport.send_text(self.admin, text), chat_id, None)  # reply here → client
