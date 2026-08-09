"""Error404 Telegram Bot — entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys

from telegram import BotCommand, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import handlers
from bot.chain import ChainClient
from bot.config import load_settings, setup_logging
from bot.health import start_health_server
from bot.models import init_db
from bot.monitor import ChainMonitor

log = logging.getLogger(__name__)


async def validate_telegram(app: Application, group_chat_id: int) -> None:
    try:
        me = await app.bot.get_me()
        log.info("Authenticated as @%s (%s)", me.username, me.id)
    except InvalidToken:
        log.critical("Invalid token – check BotFather")
        sys.exit(1)
    except TelegramError as exc:
        log.critical("Telegram getMe failed: %s", exc)
        sys.exit(1)

    try:
        chat = await app.bot.get_chat(group_chat_id)
        log.info("Group verified: %s (%s)", chat.title, chat.id)
    except TelegramError as exc:
        log.critical("GROUP_CHAT_ID %s is unreachable: %s", group_chat_id, exc)
        sys.exit(1)


async def main_async() -> None:
    setup_logging()
    cfg = load_settings()

    init_db(cfg.database_url)
    start_health_server(cfg.port)

    app = ApplicationBuilder().token(cfg.bot_token).concurrent_updates(True).build()
    app.bot_data["settings"] = cfg

    await validate_telegram(app, cfg.group_chat_id)

    chain = ChainClient(cfg.rpc_url, cfg.chain_id, cfg.contract_address)
    if not await chain.connect():
        log.error("Starting without a confirmed RPC connection; monitor will keep retrying.")
    app.bot_data["chain"] = chain

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("wallet", handlers.wallet))
    app.add_handler(CommandHandler("contract", handlers.contract))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.welcome_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.moderate))
    app.add_error_handler(handlers.error_handler)

    await app.bot.set_my_commands([
        BotCommand("start", "Create your Error404 wallet"),
        BotCommand("wallet", "Show your address & balances"),
        BotCommand("contract", "Error404 token contract"),
        BotCommand("help", "How this bot works"),
    ])

    monitor = ChainMonitor(app.bot, chain, cfg)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    monitor_task = asyncio.create_task(monitor.run())

    log.info("Error404 Bot is live 🚀")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Shutting down…")
        monitor.stop()
        monitor_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
