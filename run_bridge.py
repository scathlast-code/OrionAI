"""Запуск ботів у WhatsApp або Instagram через міст.

    python run_bridge.py whatsapp ai        # ШІ-бот у WhatsApp
    python run_bridge.py whatsapp manager   # головний бот у WhatsApp
    python run_bridge.py instagram ai       # ШІ-бот в Instagram
    python run_bridge.py instagram manager  # головний бот в Instagram

Те саме можна задати змінними BRIDGE_PLATFORM і BRIDGE_BOT (зручно на хостингу).
"""

import asyncio
import logging
import os
import sys

import uvicorn

import bridge as B

log = logging.getLogger("run_bridge")


def main():
    platform = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BRIDGE_PLATFORM", "whatsapp")).lower()
    which = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("BRIDGE_BOT", "ai")).lower()
    if platform not in ("whatsapp", "instagram") or which not in ("ai", "manager"):
        raise SystemExit(__doc__)
    db_env = "MANAGER_DB_PATH" if which == "manager" else "DB_PATH"
    data_dir = os.path.dirname(os.environ.get(db_env, "data/bot.db")) or "data"
    public = os.environ.get("MANAGER_PUBLIC_URL" if which == "manager" else "PUBLIC_URL", os.environ.get("PUBLIC_URL", ""))
    br = B.WhatsApp(data_dir) if platform == "whatsapp" else B.Instagram(data_dir, public)
    br.load_state()
    br.install()
    if platform == "instagram" and not public:
        raise SystemExit("Для Instagram потрібна PUBLIC_URL: Instagram забирає картинки й аудіо за посиланням.")
    # токен Telegram не потрібен — міст підставляє свій
    os.environ.setdefault("MANAGER_TOKEN" if which == "manager" else "TELEGRAM_TOKEN", "1:bridge")
    os.environ["BRIDGE_PLATFORM"] = platform
    asyncio.run(run_manager(br) if which == "manager" else run_ai(br))


async def run_ai(br):
    import bot

    bot.init_db()
    if not bot.MCP_SECRET or len(bot.MCP_SECRET) < 16:
        raise SystemExit("Задайте MCP_SECRET (від 16 символів).")
    app = bot.mcp.streamable_http_app(streamable_http_path=f"/{bot.MCP_SECRET}/mcp", stateless_http=True, host="0.0.0.0")
    app.router.routes.extend(B.routes(br))
    br.app = bot.tg_app
    br.community, bot.CM.bridge, bot.CM.bot = bot.CM, br, bot.tg_app.bot  # 🏘 групи WhatsApp / коментарі Instagram
    await bot.tg_app.initialize()
    await bot.tg_app.start()
    bot.BACKGROUND.add(asyncio.create_task(bot.task_worker()))
    if isinstance(br, B.Instagram) and bot.ALLOWED_USERS:
        await br.setup_menu([t for row in bot.L.reply_rows(bot.ALLOWED_USERS[0]) for t in row])
    log.info("ШІ-бот працює через %s", br.platform)
    auto_webhook(br, os.environ.get("PUBLIC_URL", ""))
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=bot.PORT, log_level="info")).serve()


async def run_manager(br):
    import manager

    manager.init_db()
    if len(manager.SECRET) < 16:
        raise SystemExit("Задайте MANAGER_SECRET (від 16 символів).")
    manager.web.router.routes.extend(B.routes(br))
    br.app = manager.app
    br.community, manager.CM.bridge, manager.CM.bot = manager.CM, br, manager.app.bot
    await manager.app.initialize()
    await manager.app.start()
    task = asyncio.create_task(manager.monitor_loop())
    if isinstance(br, B.Instagram) and manager.ALLOWED:
        await br.setup_menu([t for row in manager.L.reply_rows(manager.ALLOWED[0]) for t in row])
    log.info("Головний бот працює через %s", br.platform)
    auto_webhook(br, os.environ.get("MANAGER_PUBLIC_URL", os.environ.get("PUBLIC_URL", "")))
    try:
        await uvicorn.Server(uvicorn.Config(manager.web, host="0.0.0.0", port=manager.PORT, log_level="info")).serve()
    finally:
        task.cancel()


def auto_webhook(br, public):
    """WhatsApp: через кілька секунд після старту прив'язуємо адресу вебхука до номера цього бота."""
    if not isinstance(br, B.WhatsApp) or not public or os.environ.get("WA_AUTO_WEBHOOK", "1") == "0":
        return

    async def later():
        await asyncio.sleep(5)
        await br.register_webhook(public.rstrip("/") + "/wa/webhook")

    asyncio.get_event_loop().create_task(later())


if __name__ == "__main__":
    main()
