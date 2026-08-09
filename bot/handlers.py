from __future__ import annotations

import logging
import re
from collections import defaultdict, deque

from sqlalchemy import select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import DISCLAIMER, Settings
from .crypto_utils import create_wallet, encrypt_secret, make_referral_code
from .models import User, get_session

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
MAX_URLS = 3
REPEAT_LIMIT = 3

_recent: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=6))
_strikes: dict[int, int] = defaultdict(int)


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    tg_user = update.effective_user
    if tg_user is None:
        return
    username = tg_user.username or tg_user.first_name or "anon"

    with get_session() as session:
        existing = session.scalar(select(User).where(User.telegram_id == tg_user.id))
        if existing:
            await update.effective_message.reply_text(
                f"👋 You already have an Error404 wallet:\n`{existing.wallet_address}`\n\n"
                f"Use /wallet or /contract.\n\n{cfg.branding}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        address, private_key = create_wallet()
        encrypted = encrypt_secret(private_key, cfg.encryption_key)
        user = User(
            telegram_id=tg_user.id,
            username=username,
            wallet_address=address,
            encrypted_private_key=encrypted,
            referral_code=make_referral_code(),
        )
        session.add(user)
        session.commit()

    dm_text = (
        "🔐 *Your Error404 wallet is ready!*\n\n"
        f"*Address:* `{address}`\n"
        f"*Encrypted key (backup this!):*\n`{encrypted}`\n\n"
        "⚠️ Store this backup somewhere safe and offline.\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}"
    )
    try:
        await context.bot.send_message(tg_user.id, dm_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        log.warning("Could not DM %s: %s", tg_user.id, exc)
        await update.effective_message.reply_text(
            "I couldn't DM you — please open a chat with me and press /start again."
        )
        return

    try:
        await context.bot.send_message(
            cfg.group_chat_id,
            f"🚀 Welcome @{username}! Your Error404 wallet is live: `{address}`\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.warning("Group announcement failed: %s", exc)


async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    with get_session() as session:
        user = session.scalar(select(User).where(User.telegram_id == update.effective_user.id))
    if not user:
        await update.effective_message.reply_text("No wallet yet — send /start to create one.")
        return

    chain = context.application.bot_data.get("chain")
    balance_line = ""
    if chain:
        native = await chain.native_balance(user.wallet_address)
        token = await chain.token_balance(user.wallet_address)
        balance_line = f"\n💰 {native:.4f} RH | {token:,.2f} $ERROR404"

    await update.effective_message.reply_text(
        f"👛 *Your wallet*\n`{user.wallet_address}`{balance_line}\n"
        f"[View on explorer]({cfg.address_link(user.wallet_address)})\n\n{cfg.branding}",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    await update.effective_message.reply_text(
        f"📜 *Error404 ($ERROR404)* — ERC-404 on Robinhood Chain (id {cfg.chain_id})\n"
        f"`{cfg.contract_address}`\n"
        f"[View on explorer]({cfg.address_link(cfg.contract_address)})\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    await update.effective_message.reply_text(
        "🤖 *Error404 Bot* — the official community bot on Robinhood Chain.\n\n"
        "/start – create your wallet\n"
        "/wallet – your address & balances\n"
        "/contract – token contract + explorer\n"
        "/help – how this bot works\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    for member in update.message.new_chat_members or []:
        if member.is_bot:
            continue
        text = (
            f"👋 Welcome to Error404, {member.first_name}!\n\n"
            "Send /start to get your free on-chain wallet.\n\n"
            f"🌐 {cfg.project_website}\n🐦 https://x.com/erro404hood\n"
            f"💬 https://t.me/error404groupofficial\n\n{DISCLAIMER}"
        )
        try:
            await context.bot.send_message(member.id, text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(
                f"👋 Welcome @{member.username or member.first_name}! "
                f"DM me and send /start to claim your Error404 wallet.\n{cfg.branding}"
            )


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _settings(context)
    msg = update.effective_message
    if msg is None or not msg.text or update.effective_chat.type == "private":
        return

    uid = update.effective_user.id
    text = msg.text.strip()
    reason: str | None = None

    if len(URL_RE.findall(text)) > MAX_URLS:
        reason = "too many links"

    _recent[uid].append(text.lower())
    if list(_recent[uid]).count(text.lower()) > REPEAT_LIMIT:
        reason = "repeated spam"

    found = ADDRESS_RE.findall(text)
    if found and any(a.lower() != cfg.contract_address.lower() for a in found):
        reason = "unverified contract address"

    if reason is None:
        return

    try:
        await msg.delete()
    except Exception as exc:
        log.warning("Could not delete message: %s", exc)

    _strikes[uid] += 1
    if _strikes[uid] >= 3:
        await context.bot.send_message(
            cfg.group_chat_id,
            f"🚫 @{update.effective_user.username or uid} has 3 strikes ({reason}). Admins, please review.",
        )
    else:
        await context.bot.send_message(
            cfg.group_chat_id,
            f"⚠️ @{update.effective_user.username or uid} message removed ({reason}) — strike {_strikes[uid]}/3.",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled Telegram error: %s", context.error)
