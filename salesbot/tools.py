"""Operator commands. Run from the repository root:

    python -m salesbot.tools check-config            validate the environment and the shop data (deploy build step)
    python -m salesbot.tools shop upload <folder>    validate a shop folder and store it in the database (SHOP_SOURCE=db)
    python -m salesbot.tools shop show               what the database currently holds
    python -m salesbot.tools photos warm --chat ID   upload every uncached photo once (a private channel or group)
    python -m salesbot.tools photos import FILE      reuse file_ids from an old photos.json of the same bot
    python -m salesbot.tools webhook info|set|delete Telegram webhook status (secret masked) / point it here / remove it
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot

from .catalog import Catalog
from .config import BASE_DIR, Settings, get_settings
from .llm import build_system, reply_tool
from .photos import PhotoCache
from .shop import ShopData, ShopDataError
from .storage import Storage
from .templates import Templates
from .transport import BotTransport, OutPhoto

SECONDS_PER_PHOTO = 3.2  # Telegram allows ~20 messages per minute into one group or channel


def assemble(shop: ShopData, settings: Settings) -> Catalog:
    """Build everything the bot builds at startup, so a broken shop fails here and not in production."""
    catalog = Catalog(shop.catalog, shop.synonyms)
    templates = Templates(shop.content, catalog, settings.persona_name)
    build_system(settings, catalog, shop.content, shop.examples)
    reply_tool(shop.content, settings.max_parts)
    for slug in catalog.products:
        templates.render({"template_id": "product_card", "product_slug": slug})
    return catalog


async def with_storage(settings: Settings, fn: Callable[[Storage], Awaitable[int]]) -> int:
    storage = await Storage.connect(settings.database_url)
    try:
        return await fn(storage)
    finally:
        await storage.close()


async def cmd_check_config(args: argparse.Namespace, s: Settings) -> int:
    async def run(storage: Storage) -> int:
        shop = await ShopData.load(s.shop_dir, storage)
        catalog = assemble(shop, s)
        cache = await PhotoCache(storage).load()
        srcs = catalog.all_photo_sources()
        print(f"OK: settings complete · shop from {shop.source}: catalog {catalog.version}, "
              f"{len(catalog.products)} products · model {s.model} · "
              f"photos cached {sum(src in cache.ids for src in srcs)}/{len(srcs)} (the rest upload on first use)")
        return 0
    return await with_storage(s, run)


async def cmd_shop(args: argparse.Namespace, s: Settings) -> int:
    async def run(storage: Storage) -> int:
        if args.action == "upload":
            folder = (BASE_DIR / args.folder).resolve()
            texts = ShopData.read_folder(folder)
            shop = ShopData.parse(texts, str(folder))
            catalog = assemble(shop, s)
            await ShopData.upload(storage, texts)
            print(f"uploaded {len(texts)} files from {folder}: catalog {catalog.version}, {len(catalog.products)} products")
            return 0
        rows = await storage.pool.fetch("SELECT name, length(body) AS size, updated_at FROM shop_files ORDER BY name")
        for r in rows:
            print(f"  {r['name']:<14} {r['size']:>9} chars  {r['updated_at']:%Y-%m-%d %H:%M}")
        print("no shop data in the database" if not rows else "")
        return 0 if rows else 1
    return await with_storage(s, run)


async def cmd_photos(args: argparse.Namespace, s: Settings) -> int:
    async def run(storage: Storage) -> int:
        shop = await ShopData.load(s.shop_dir, storage)
        catalog = Catalog(shop.catalog, shop.synonyms)
        cache = await PhotoCache(storage).load()
        if args.action == "import":
            legacy = json.loads(await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8"))
            n = await cache.import_legacy(legacy,
                                         catalog.all_photo_sources())
            srcs = catalog.all_photo_sources()
            covered = sum(src in cache.ids for src in srcs)
            print(f"shop from {shop.source}: imported {n} file_id(s); {covered}/{len(srcs)} of its photos cached")
            if not covered:
                print("nothing matched this shop's photos: is SHOP_SOURCE the shop these file_ids belong to?",
                      file=sys.stderr)
                return 1
            return 0
        todo = [src for src in catalog.all_photo_sources() if src not in cache.ids][: args.limit or None]
        print(f"{len(todo)} photo(s) to upload to chat {args.chat}")
        bot = Bot(s.telegram_bot_token)
        transport, failed = BotTransport(bot), 0
        try:
            for i in range(0, len(todo), 10):
                batch = [OutPhoto(src, data=data) for src in todo[i:i + 10] if (data := await cache.fetch(src))]
                failed += min(10, len(todo) - i) - len(batch)
                if not batch:
                    continue
                started = time.monotonic()
                ids = await transport.send_photos(args.chat, batch)
                for p, fid in zip(batch, ids, strict=False):
                    if fid:
                        await cache.remember(p.src, fid)
                print(f"  {min(i + 10, len(todo))}/{len(todo)}")
                await asyncio.sleep(max(0.0, started + SECONDS_PER_PHOTO * len(batch) - time.monotonic()))
        finally:
            await bot.session.close()
        print(f"done: {len(cache.ids)} cached, {failed} source(s) could not be read")
        return 1 if failed else 0
    return await with_storage(s, run)


async def cmd_webhook(args: argparse.Namespace, s: Settings) -> int:
    bot = Bot(s.telegram_bot_token)
    try:
        if args.action == "info":
            info = (await bot.get_webhook_info()).model_dump(exclude_none=True)
            info["url"] = str(info.get("url", "")).replace(s.webhook_secret, "***")
            print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.action == "set":
            base = (args.url or s.webhook_base_url).rstrip("/")
            if not base:
                print("give --url https://<your-service> or set PUBLIC_URL", file=sys.stderr)
                return 1
            ok = await bot.set_webhook(base + s.webhook_path, secret_token=s.webhook_secret)
        else:
            ok = await bot.delete_webhook()
        print("ok" if ok else "failed")
        return 0 if ok else 1
    finally:
        await bot.session.close()


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Cyrillic output on Windows consoles
    parser = argparse.ArgumentParser(prog="python -m salesbot.tools", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config").set_defaults(func=cmd_check_config)
    shop = sub.add_parser("shop")
    shop.add_argument("action", choices=["upload", "show"])
    shop.add_argument("folder", nargs="?", default="data/sample")
    shop.set_defaults(func=cmd_shop)
    ph = sub.add_parser("photos")
    ph.add_argument("action", choices=["warm", "import"])
    ph.add_argument("file", nargs="?", help="for import: an old photos.json")
    ph.add_argument("--chat", type=int, help="for warm: the chat to upload into (a private channel or group)")
    ph.add_argument("--limit", type=int, default=0, help="for warm: upload at most this many (0 = all)")
    ph.set_defaults(func=cmd_photos)
    wh = sub.add_parser("webhook")
    wh.add_argument("action", choices=["info", "set", "delete"])
    wh.add_argument("--url", help="public base URL for `set`, e.g. https://<your-service>.onrender.com")
    wh.set_defaults(func=cmd_webhook)
    args = parser.parse_args()
    if args.command == "photos" and args.action == "warm" and not args.chat:
        parser.error("photos warm needs --chat")
    if args.command == "photos" and args.action == "import" and not args.file:
        parser.error("photos import needs a file")
    try:
        return asyncio.run(args.func(args, get_settings()))
    except ShopDataError as e:
        print(f"shop data error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
