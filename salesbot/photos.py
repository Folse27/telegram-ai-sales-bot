"""Lazy photo cache (cache-aside).

Telegram lets a bot resend a file it already uploaded by its file_id: instant, nothing re-uploaded. A file_id only
works for the bot that uploaded it, so each bot has to upload every image once. Here that happens the first time
a photo is actually sent; the file_id from Telegram's answer is stored and used from then on. `python -m
salesbot.tools photos warm --chat <id>` does the same in advance so the first client doesn't wait.

Image sources are URLs or paths relative to the repository (the sample shop ships its images in data/sample).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx
from aiogram.exceptions import TelegramBadRequest

from .config import BASE_DIR
from .storage import Storage
from .transport import OutPhoto, Transport

log = logging.getLogger("salesbot")

MAX_BYTES = 10 * 1024 * 1024  # Telegram's photo upload limit
Fetch = Callable[[str], Awaitable[bytes | None]]


async def fetch_source(src: str) -> bytes | None:
    """Image bytes from a URL or a repository path; None (logged) if it can't be read."""
    try:
        if src.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": "salesbot-photo-cache/1.0"}) as client:
                r = await client.get(src)
                r.raise_for_status()
                data = r.content
        else:
            path = (BASE_DIR / src).resolve()
            if BASE_DIR not in path.parents:  # sources come from shop data: never read outside the repository
                raise ValueError("path outside the repository")
            data = path.read_bytes()
    except (httpx.HTTPError, OSError, ValueError) as e:
        log.warning("photo %s can't be read: %s", src, e)
        return None
    if len(data) > MAX_BYTES:
        log.warning("photo %s is %d KB, over Telegram's 10 MB limit", src, len(data) // 1024)
        return None
    return data


class PhotoCache:
    def __init__(self, storage: Storage, fetch: Fetch = fetch_source):
        self.storage, self.fetch = storage, fetch
        self.ids: dict[str, str] = {}

    async def load(self) -> PhotoCache:
        self.ids = {r["src"]: r["file_id"] for r in await self.storage.pool.fetch("SELECT src, file_id FROM photo_cache")}
        return self

    async def remember(self, src: str, file_id: str) -> None:
        self.ids[src] = file_id
        await self.storage.pool.execute(
            "INSERT INTO photo_cache (src, file_id) VALUES ($1, $2) ON CONFLICT (src) DO UPDATE SET file_id = $2",
            src, file_id)

    async def forget(self, srcs: list[str]) -> None:
        for src in srcs:
            self.ids.pop(src, None)
        await self.storage.pool.execute("DELETE FROM photo_cache WHERE src = ANY($1::text[])", srcs)

    async def _prepare(self, srcs: list[str]) -> list[OutPhoto]:
        out = []
        for src in srcs:
            if src in self.ids:
                out.append(OutPhoto(src, file_id=self.ids[src]))
            elif (data := await self.fetch(src)) is not None:
                out.append(OutPhoto(src, data=data))
        return out

    async def send(self, transport: Transport, chat_id: int, srcs: list[str], caption: str) -> bool:
        """Send the photos (cached ones by file_id, the rest uploaded). False if none could be sent."""
        photos = await self._prepare(srcs)
        if not photos:
            return False
        try:
            ids = await transport.send_photos(chat_id, photos, caption=caption)
        except TelegramBadRequest as e:
            cached = [p.src for p in photos if p.file_id]
            if not cached or "file" not in str(e).lower():
                raise
            # A stored file_id stopped working (e.g. the bot token changed): upload these again, once.
            log.warning("cached photos rejected (%s); re-uploading %d", e, len(cached))
            await self.forget(cached)
            photos = await self._prepare(srcs)
            if not photos:
                return False
            ids = await transport.send_photos(chat_id, photos, caption=caption)
        for p, fid in zip(photos, ids, strict=False):
            if p.file_id is None and fid:
                await self.remember(p.src, fid)
        return True

    async def import_legacy(self, photos_json: dict, catalog_sources: list[str]) -> int:
        """Seed the cache from an old photos.json ({key: {file_id, src}}) of the same bot. Returns rows written."""
        wanted, n = set(catalog_sources), 0
        for v in photos_json.values():
            if v.get("src") in wanted and v.get("file_id") and v["src"] not in self.ids:
                await self.remember(v["src"], v["file_id"])
                n += 1
        return n
