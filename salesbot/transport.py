"""Everything the bot sends to Telegram goes through a Transport; tests swap in a recording fake."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto, LinkPreviewOptions, Message

log = logging.getLogger("salesbot")


@dataclass
class OutPhoto:
    """A photo to send: a cached Telegram file_id, or the image bytes for a first upload."""

    src: str
    file_id: str | None = None
    data: bytes | None = None

    @property
    def filename(self) -> str:
        return self.src.rsplit("/", 1)[-1].split("?")[0] or "photo.jpg"


class Transport(Protocol):
    async def send_text(self, chat_id: int, text: str, *, reply_markup: Any = None, reply_to: int | None = None) -> int: ...
    async def send_photos(self, chat_id: int, photos: list[OutPhoto], caption: str | None = None) -> list[str | None]: ...
    async def send_typing(self, chat_id: int) -> None: ...
    async def copy_message(self, to_chat_id: int, from_chat_id: int, message_id: int,
                           reply_to: int | None = None) -> int: ...
    async def edit_markup(self, chat_id: int, message_id: int, markup: Any) -> None: ...


def _largest_file_id(m: Message | None) -> str | None:
    return m.photo[-1].file_id if m and m.photo else None


class BotTransport:
    """Real Telegram via aiogram. Texts and captions are HTML; link previews are off."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:  # flood control: wait as told, then try once more
            await asyncio.sleep(e.retry_after + 1)
            return await fn(*args, **kwargs)
        except TelegramForbiddenError:
            log.warning("chat %s blocked the bot", args[0] if args else kwargs.get("chat_id"))
            return None

    async def send_text(self, chat_id: int, text: str, *, reply_markup: Any = None, reply_to: int | None = None) -> int:
        m = await self._call(self.bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=reply_markup,
                             reply_to_message_id=reply_to, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return m.message_id if m else 0

    async def send_photos(self, chat_id: int, photos: list[OutPhoto], caption: str | None = None) -> list[str | None]:
        """One photo or an album (up to 10). Returns Telegram's file_id for each photo, in order."""
        def media(p: OutPhoto) -> str | BufferedInputFile:
            return p.file_id or BufferedInputFile(p.data or b"", filename=p.filename)

        if len(photos) == 1:
            m = await self._call(self.bot.send_photo, chat_id, media(photos[0]), caption=caption, parse_mode="HTML")
            return [_largest_file_id(m)]
        group = [InputMediaPhoto(media=media(p), caption=caption if i == 0 else None, parse_mode="HTML")
                 for i, p in enumerate(photos)]
        msgs = await self._call(self.bot.send_media_group, chat_id, group) or []
        return [_largest_file_id(m) for m in msgs]

    async def send_typing(self, chat_id: int) -> None:
        await self._call(self.bot.send_chat_action, chat_id, "typing")

    async def copy_message(self, to_chat_id: int, from_chat_id: int, message_id: int, reply_to: int | None = None) -> int:
        m = await self._call(self.bot.copy_message, to_chat_id, from_chat_id, message_id, reply_to_message_id=reply_to)
        return m.message_id if m else 0

    async def edit_markup(self, chat_id: int, message_id: int, markup: Any) -> None:
        try:
            await self._call(self.bot.edit_message_reply_markup, chat_id=chat_id, message_id=message_id,
                             reply_markup=markup)
        except Exception:  # e.g. "message is not modified": cosmetic only
            log.debug("edit_markup failed", exc_info=True)
