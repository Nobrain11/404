"""Admin notification helpers — new users, wallet events, whale alerts."""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.constants import ParseMode

from .config import Settings
from .crypto_utils import short_addr

log = logging.getLogger(__name__)


async def _notify_admins(bot: Bot, admin_ids: list[int], text: str) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.MARKDOWN,
                                   disable_web_page_preview=True)
        except Exception as exc:
            log.warning("Admin notify failed for %s: %s", admin_id, exc)


async def notify_new_user(bot: Bot, cfg: Settings, username: str,
                          telegram_id: int, wallet: str,
                          referred_by: str | None = None) -> None:
    ref_line = f"\n🔗 Referred by: *@{referred_by}*" if referred_by else ""
    text = (
        "🆕 *New User Registered*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Username: *@{username}*\n"
        f"🆔 Telegram ID: `{telegram_id}`\n"
        f"👛 Wallet: `{wallet}`\n"
        f"🔍 [Explorer]({cfg.address_link(wallet)})"
        f"{ref_line}"
    )
    await _notify_admins(bot, cfg.admin_ids, text)


async def notify_wallet_created(bot: Bot, cfg: Settings, username: str,
                                 telegram_id: int, wallet: str) -> None:
    text = (
        "🔐 *New Wallet Created*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *@{username}* (`{telegram_id}`)\n"
        f"👛 Address: `{wallet}`\n"
        f"🔍 [Explorer]({cfg.address_link(wallet)})"
    )
    await _notify_admins(bot, cfg.admin_ids, text)


async def notify_wallet_imported(bot: Bot, cfg: Settings, username: str,
                                  telegram_id: int, wallet: str,
                                  method: str) -> None:
    text = (
        "📥 *Wallet Imported*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *@{username}* (`{telegram_id}`)\n"
        f"📝 Method: *{method}*\n"
        f"👛 Address: `{wallet}`\n"
        f"🔍 [Explorer]({cfg.address_link(wallet)})"
    )
    await _notify_admins(bot, cfg.admin_ids, text)


async def notify_trade(bot: Bot, cfg: Settings, username: str,
                        trade_type: str, amount: str, wallet: str,
                        tx_hash: str) -> None:
    icon = "🟢" if trade_type == "BUY" else "🔴"
    text = (
        f"{icon} *{trade_type} Executed*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *@{username}*\n"
        f"👛 `{short_addr(wallet)}`\n"
        f"💱 Amount: *{amount}*\n"
        f"🔍 [View Tx]({cfg.tx_link(tx_hash)})"
    )
    await _notify_admins(bot, cfg.admin_ids, text)


async def notify_whale(bot: Bot, cfg: Settings, tx_hash: str,
                        sender: str, recipient: str, amount: float) -> None:
    text = (
        "🐋 *Whale Transaction Detected*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💸 Amount: *{amount:,.4f} {cfg.token_symbol}*\n"
        f"📤 From: `{short_addr(sender)}`\n"
        f"📥 To: `{short_addr(recipient)}`\n"
        f"🔍 [View Tx]({cfg.tx_link(tx_hash)})"
    )
    await _notify_admins(bot, cfg.admin_ids, text)


async def notify_referral(bot: Bot, cfg: Settings, referrer: str,
                           new_user: str) -> None:
    text = (
        "🤝 *Referral Conversion*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Referrer: *@{referrer}*\n"
        f"🆕 New user: *@{new_user}*\n"
        f"💰 Bonus: *{cfg.referral_bonus_amount} {cfg.token_symbol}*"
    )
    await _notify_admins(bot, cfg.admin_ids, text)
