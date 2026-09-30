"""Обрій Тех demo sales-manager bot: everything that runs on the server (PLAN.md §3).

Run on Render:        python main.py              (webhook, aiohttp)
Run locally (dev):    python main.py --polling    (deletes the webhook, long-polls)
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

from config import Settings, get_settings

log = logging.getLogger("Обрій")


# ═══════════════════════════ 1. Storage ═══════════════════════════

async def create_pool(dsn: str) -> asyncpg.Pool:
    # No idle connections are kept (min_size=0, closed after 60 s idle) so Neon can scale to zero.
    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=5, max_inactive_connection_lifetime=60, command_timeout=15
    )


async def open_storage(settings: Settings) -> asyncpg.Pool | None:
    if not settings.database_url:
        log.warning("DATABASE_URL is empty: storage disabled")
        return None
    pool = await create_pool(settings.database_url)
    version = await pool.fetchval("SELECT version()")
    log.info("database ok: %s", version.split(",")[0])
    return pool


# ═══════════════════════════ 9. Telegram ═══════════════════════════

router = Router()


@router.message(F.chat.type == "private", F.text)
async def echo(message: Message) -> None:
    # Step 1 placeholder: replaced by the SessionManager in Step 4.
    await message.answer(f"echo: {message.text}")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


# ═══════════════════════════ 10. App ═══════════════════════════

POOL = web.AppKey("pool", object)


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
        app[POOL] = await open_storage(settings)
        await bot.set_webhook(
            settings.webhook_base_url + settings.webhook_path,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("webhook set: %s/tg/***", settings.webhook_base_url)

    async def on_cleanup(app: web.Application) -> None:
        if app.get(POOL):
            await app[POOL].close()
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def run_polling(settings: Settings, bot: Bot, dp: Dispatcher) -> None:
    pool = await open_storage(settings)
    try:
        await bot.delete_webhook()
        log.info("webhook deleted, polling. To hand back to Render: python scrape.py webhook set")
        await dp.start_polling(bot)
    finally:
        if pool:
            await pool.close()


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
        web.run_app(build_app(settings, bot, dp), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
