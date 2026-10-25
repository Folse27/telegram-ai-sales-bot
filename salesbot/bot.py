"""Runtime wiring and the Telegram handlers (aiogram router)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, Update

from .catalog import Catalog
from .config import Settings
from .handoff import Handoff
from .llm import LLM
from .orders import Orders
from .photos import PhotoCache
from .pipeline import LlmPipeline
from .sessions import Incoming, SessionConfig, SessionManager
from .shop import ShopData
from .storage import Storage
from .templates import Templates
from .transport import BotTransport, Transport
from .validation import Validator

log = logging.getLogger("salesbot")


@dataclass
class Runtime:
    """Everything handlers need; put into the dispatcher as dp["rt"]."""

    settings: Settings
    storage: Storage
    transport: Transport
    sessions: SessionManager
    templates: Templates
    handoff: Handoff

    def allowed(self, user_id: int) -> bool:
        return not self.settings.allowed_user_ids or user_id in self.settings.allowed_user_ids


async def build_runtime(settings: Settings, bot: Bot) -> Runtime:
    storage = await Storage.connect(settings.database_url)
    await storage.cleanup()
    log.info("database ok: %s", await storage.version())
    shop = await ShopData.load(settings.shop_dir, storage)
    transport = BotTransport(bot)
    catalog = Catalog(shop.catalog, shop.synonyms)
    templates = Templates(shop.content, catalog, settings.persona_name)
    llm = LLM(settings, catalog, shop.content, shop.examples)
    orders = Orders(catalog, templates)
    handoff = Handoff(settings, storage, transport, templates, orders)
    photos = await PhotoCache(storage).load()
    pipeline = LlmPipeline(settings, storage, transport, catalog, templates,
                           Validator(catalog, shop.content, settings.max_parts), llm, orders, handoff, photos)
    sessions = SessionManager(pipeline, transport, SessionConfig.from_settings(settings))
    log.info("shop %s: catalog %s, %d products, %d cached photos; model %s (%s); %d tester(s)",
             shop.source, catalog.version, len(catalog.products), len(photos.ids), settings.model, llm.params,
             len(settings.tester_ids))
    rt = Runtime(settings, storage, transport, sessions, templates, handoff)

    async def on_resume(chat_id: int) -> None:
        await resume_chat(rt, chat_id)

    handoff.on_resume = on_resume
    return rt


async def resume_chat(rt: Runtime, chat_id: int) -> None:
    """Back in BOT mode: let the bot answer whatever the client wrote after the last bot/manager message."""
    for r in await rt.handoff.trailing_client_messages(chat_id):
        await rt.sessions.on_message(Incoming(chat_id, r["text"], r["id"], r["tg_message_id"] or 0))


async def reschedule_unanswered(rt: Runtime) -> None:
    """After a restart, answer user messages that arrived in the last 2 min but were never answered."""
    rows = await rt.storage.unanswered_recent(120)
    for r in rows:
        await rt.sessions.on_message(Incoming(r["chat_id"], r["text"], r["id"], r["tg_message_id"] or 0))
    if rows:
        log.info("rescheduled %d unanswered message(s)", len(rows))


router = Router()


async def _accept(message: Message, event_update: Update, rt: Runtime) -> bool:
    """Allowlist + update dedup + client upsert, shared by all private-chat handlers."""
    user = message.from_user
    if not user or not rt.allowed(user.id) or not await rt.storage.first_seen(event_update.update_id):
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


@router.message(F.chat.type == "private", Command("clear"))
async def on_clear(message: Message, event_update: Update, rt: Runtime) -> None:
    """Testers only: wipe the bot's memory of this chat. For anyone else /clear is an ordinary message."""
    if not message.from_user or message.from_user.id not in rt.settings.tester_ids:
        raise SkipHandler()  # checked before dedup, so the text handler still gets this update
    if not await _accept(message, event_update, rt):
        return
    rt.sessions.drop(message.chat.id)
    await rt.storage.wipe_chat(message.chat.id)
    await rt.transport.send_text(message.chat.id, "🧹 Пам'ять бота для цього чату очищено: повідомлення, замовлення "
                                                  "й запити до менеджера. Напишіть /start, щоб почати заново.")


@router.message(F.chat.type == "private", F.text)
async def on_private_text(message: Message, event_update: Update, rt: Runtime) -> None:
    if not message.text or not await _accept(message, event_update, rt):
        return
    db_id = await rt.storage.save_message(message.chat.id, "user", message.text, message.message_id, answered=False)
    await rt.sessions.on_message(Incoming(message.chat.id, message.text, db_id, message.message_id))


@router.message(F.chat.type == "private")
async def on_private_other(message: Message, event_update: Update, rt: Runtime) -> None:
    """Photos, voice, stickers…: relayed to staff in HUMAN mode, otherwise the bot reads text only."""
    if not await _accept(message, event_update, rt):
        return
    if await rt.handoff.is_human(message.chat.id):
        await rt.handoff.relay_client(message.chat.id, message.message_id)
    else:
        await rt.transport.send_text(message.chat.id, rt.templates.text("unsupported_media").text)


# ── staff group: handoff buttons, manager replies, commands ──

@router.callback_query(F.data.startswith("ho:"))
async def on_handoff_button(cb: CallbackQuery, rt: Runtime) -> None:
    if not cb.message or cb.message.chat.id != rt.settings.admin_chat_id or not cb.data:
        await cb.answer()
        return
    _, action, hid = cb.data.split(":")
    await cb.answer(await rt.handoff.button(int(hid), action, cb.from_user.full_name))


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_admin_message(message: Message, event_update: Update, rt: Runtime) -> None:
    if message.chat.id != rt.settings.admin_chat_id or not await rt.storage.first_seen(event_update.update_id):
        return
    text = message.text or message.caption or ""
    if text.startswith("/"):
        await rt.transport.send_text(message.chat.id, await rt.handoff.command(text), reply_to=message.message_id)
    elif message.reply_to_message:  # a reply to a card or a relayed client message goes to that client
        chat_id = await rt.handoff.manager_reply(message.reply_to_message.message_id, message.message_id,
                                                 text or "[медіа від менеджера]")
        if chat_id is None:  # say so instead of silently dropping the manager's answer
            await rt.transport.send_text(message.chat.id, "⚠️ Не знаю, якому клієнту це надіслати. Відповідайте "
                                         "реплаєм на картку запиту або на переслане повідомлення клієнта.",
                                         reply_to=message.message_id)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
