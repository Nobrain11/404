"""Error404 Telegram Bot — entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys

from telegram import BotCommand, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters,
)

from bot import handlers
from bot.chain import ChainClient
from bot.config import load_settings, setup_logging
from bot.health import start_health_server
from bot.models import init_db
from bot.monitor import ChainMonitor
from bot.scheduler import build_scheduler

log = logging.getLogger(__name__)


async def validate_telegram(app, group_chat_id: int) -> None:
    try:
        me = await app.bot.get_me()
        log.info("Authenticated as @%s (%s)", me.username, me.id)
    except InvalidToken:
        log.critical("Invalid BOT_TOKEN — check BotFather")
        sys.exit(1)
    except TelegramError as exc:
        log.critical("Telegram getMe failed: %s", exc)
        sys.exit(1)

    try:
        chat = await app.bot.get_chat(group_chat_id)
        log.info("Group verified: %s (%s)", chat.title, chat.id)
    except TelegramError as exc:
        log.critical("GROUP_CHAT_ID %s unreachable — is bot an admin? (%s)", group_chat_id, exc)
        sys.exit(1)


async def main_async() -> None:
    setup_logging()
    cfg = load_settings()

    init_db(cfg.database_url)
    start_health_server(cfg.port)

    app = ApplicationBuilder().token(cfg.bot_token).concurrent_updates(True).build()
    app.bot_data["settings"] = cfg

    await validate_telegram(app, cfg.group_chat_id)

    chain = ChainClient(
        cfg.rpc_url, cfg.chain_id, cfg.contract_address,
        cfg.weth_address, cfg.swap_router, cfg.token_decimals,
    )
    if not await chain.connect():
        log.error("RPC not confirmed — monitor will retry.")
    app.bot_data["chain"] = chain

    app.add_handler(CommandHandler("start",    handlers.start))
    app.add_handler(CommandHandler("wallet",   handlers.wallet_cmd))
    app.add_handler(CommandHandler("contract", handlers.contract_cmd))
    app.add_handler(CommandHandler("help",     handlers.help_cmd))
    app.add_handler(CommandHandler("alert",    handlers.alert_cmd))
    app.add_handler(CallbackQueryHandler(handlers.callback_handler))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.welcome_new_members
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handlers.text_handler
    ))
    app.add_error_handler(handlers.error_handler)

    await app.bot.set_my_commands([
        BotCommand("start",    "Live stats + wallet setup"),
        BotCommand("wallet",   "Address, balances & recent txs"),
        BotCommand("contract", "Token contract info"),
        BotCommand("alert",    "Set a price alert"),
        BotCommand("help",     "Commands & tutorial"),
    ])

    monitor   = ChainMonitor(app.bot, chain, cfg)
    scheduler = build_scheduler(app.bot, cfg, chain)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    scheduler.start()
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
        scheduler.shutdown(wait=False)
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
