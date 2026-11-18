"""Run the Telegram bot, its Mini App API and notification scheduler together."""
import asyncio
import logging
import os
from pathlib import Path
import signal

import aiohttp
from aiohttp import web

from .api import install_api
from .links import mini_app_url
from .bot import Telegram, TelegramError, run_bot
from .scheduler import run_scheduler
from .store import Store
from .widgets import WIDGET_SCRIPT

CONFIG = Path.home() / ".config/telegram-agent"
WEB = WIDGET_SCRIPT.parent / "web"
log = logging.getLogger("aleph")


def secret(name, filename):
    value = os.getenv(name, "").strip()
    if not value:
        path = Path(os.getenv(name + "_FILE") or CONFIG / filename)
        if path.is_file():
            value = path.read_text().strip()
    if not value:
        raise SystemExit(f"Set {name} or {name}_FILE.")
    return value


async def update_menu(tg):
    current = ""
    while True:
        try:
            url = mini_app_url()
            if url and url != current:
                await tg.call("setChatMenuButton", menu_button={"type": "web_app", "text": "Aleph", "web_app": {"url": url}})
                current = url
                log.info("Telegram Mini App menu configured")
        except (OSError, ValueError, TelegramError) as exc:
            log.warning("Menu update failed (%s)", type(exc).__name__)
        await asyncio.sleep(30)


def application(store, token):
    app = web.Application(client_max_size=16384)
    install_api(app, store, token)

    async def health(_):
        store.db.execute("SELECT 1")
        return web.json_response({"status": "ok"}, headers={"Cache-Control": "no-store"})

    async def asset(request):
        filename = request.match_info.get("filename", "index.html")
        path = WEB / filename
        if not path.is_file() or path.suffix not in {".html", ".css", ".js", ".webp", ".svg", ".png", ".woff2", ".txt"}:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                                             "Cache-Control": "no-cache"})
    app.router.add_get("/healthz", health)
    app.router.add_get("/", asset)
    app.router.add_get(r"/{filename:[a-zA-Z0-9_.-]+}", asset)
    return app


async def main():
    os.umask(0o077)
    if not (WEB / "app.js").is_file() or not WIDGET_SCRIPT.is_file():
        raise SystemExit("Build Aleph's frontend with npm ci and npm run build before starting the server.")
    token = secret("TELEGRAM_BOT_TOKEN", "bot-token")
    key = secret("OPENCODE_API_KEY", "opencode-key")
    path = os.getenv("ALEPH_DB") or str(Path.home() / ".local/share/aleph/aleph.sqlite3")
    store = Store(path)
    runner = web.AppRunner(application(store, token), access_log=None)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    try:
        await runner.setup()
        await web.TCPSite(runner, os.getenv("BIND", "127.0.0.1"), int(os.getenv("PORT", "8787"))).start()
        async with aiohttp.ClientSession(trust_env=True, headers={"User-Agent":"AlephAgent/1.0"}) as session:
            tg = Telegram(session, token)
            # Long polling may only be owned by one service and cannot coexist with a webhook.
            hook = await tg.call("getWebhookInfo")
            if hook.get("url"):
                raise SystemExit("Telegram has an existing webhook. Switch delivery deliberately before starting polling.")
            log.info("Starting Telegram polling and proactive scheduler")
            jobs = [asyncio.create_task(coro) for coro in (
                run_bot(store, session, token, key), run_scheduler(store, session, token),
                update_menu(tg), stopping.wait())]
            try:
                finished, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in finished:
                    job.result()
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
    finally:
        await runner.cleanup()
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
