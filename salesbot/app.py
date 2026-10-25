"""Entry point: the aiohttp webhook server (production) or long polling (local development).

    python -m salesbot              webhook server (Render)
    python -m salesbot --polling    local: deletes the webhook and long-polls
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from .bot import build_dispatcher, build_runtime, reschedule_unanswered
from .config import Settings, get_settings
from .logs import setup_logging

log = logging.getLogger("salesbot")


class AccessLogger(AbstractAccessLogger):
    """Masks the webhook secret in the path and skips health-check noise."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if request.path == "/healthz":
            return
        path = "/tg/***" if request.path.startswith("/tg/") else request.path
        self.logger.info("%s %s %s %.0fms", request.method, path, response.status, time * 1000,
                         extra={"event": "http", "method": request.method, "path": path, "status": response.status,
                                "duration_ms": round(time * 1000)})


async def healthz(request: web.Request) -> web.Response:
    # Never touch the database here: the uptime monitor hits this every 5 min and the database must be able to sleep.
    return web.Response(text="ok")


def build_app(settings: Settings, bot: Bot, dp: Dispatcher) -> web.Application:
    if not settings.webhook_base_url:
        raise SystemExit("PUBLIC_URL / RENDER_EXTERNAL_URL is empty; use --polling for local runs")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    # Checks the X-Telegram-Bot-Api-Secret-Token header and answers 200 right away (handling runs in background).
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
        log.info("webhook deleted, polling. To hand back to the server: python -m salesbot.tools webhook set")
        await dp.start_polling(bot)
    finally:
        await rt.storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m salesbot")
    parser.add_argument("--polling", action="store_true", help="local dev: delete the webhook and long-poll")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_format)
    bot = Bot(settings.telegram_bot_token)
    dp = build_dispatcher()
    if args.polling:
        try:
            asyncio.run(run_polling(settings, bot, dp))
        except KeyboardInterrupt:  # Ctrl+C on Windows surfaces here
            log.info("stopped")
    else:
        web.run_app(build_app(settings, bot, dp), host="0.0.0.0", port=settings.port,  # noqa: S104 - a server must listen on all interfaces
                    access_log_class=AccessLogger)
