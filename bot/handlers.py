"""All Telegram command + callback + text handlers."""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque

from sqlalchemy import func, select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .admin import (
    notify_new_user, notify_referral, notify_trade,
    notify_wallet_created, notify_wallet_imported,
)
from .config import DISCLAIMER, DIVIDER, Settings
from .crypto_utils import (
    create_wallet, decrypt_secret, encrypt_secret,
    import_from_mnemonic, import_from_private_key,
    make_referral_code, short_addr,
)
from .keyboards import (
    kb_back_main, kb_buy_amounts, kb_confirm_cancel,
    kb_import_choice, kb_main, kb_orders, kb_refer,
    kb_replace_confirm, kb_sell_pct, kb_settings,
    kb_wallet_home, kb_wallet_view, kb_welcome,
)
from .models import (
    DCAJob, LimitOrder, PriceAlert, Referral,
    Transaction, User, get_session,
)
from .price import fetch_market_stats, fetch_top_holders

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
MAX_URLS = 3
REPEAT_LIMIT = 3

_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))
_strikes: dict[int, int] = defaultdict(int)

# Per-user pending trade state
_pending_buy: dict[int, float] = {}
_pending_sell: dict[int, float] = {}
_pending_limit_type: dict[int, str] = {}
_pending_limit_amount: dict[int, float] = {}
_pending_transfer_to: dict[int, str] = {}


def _cfg(ctx: ContextTypes.DEFAULT_TYPE) -> Settings:
    return ctx.application.bot_data["settings"]


def _chain(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.application.bot_data.get("chain")


def _get_user(session, tid: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == tid))


# ── Welcome screen ────────────────────────────────────────────────────────────

async def _build_welcome(cfg: Settings, chain=None) -> str:
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    icon = "🟢" if stats.change_24h >= 0 else "🔴"
    return (
        f"{DIVIDER}\n"
        f"  *ERROR 404 ({cfg.token_symbol})*\n"
        f"{DIVIDER}\n\n"
        f"💰  Price        *${stats.price_usd:.8f}*\n"
        f"📈  24h Change  {icon} *{stats.change_24h:+.2f}%*\n"
        f"📊  Market Cap  *${stats.market_cap:,.2f}*\n"
        f"💧  Liquidity   *${stats.liquidity:,.2f}*\n"
        f"🔊  24h Volume  *${stats.volume_24h:,.2f}*\n"
        f"🟢  Buys        *{stats.buys_24h}*   "
        f"🔴 Sells *{stats.sells_24h}*\n"
        f"👥  Holders     *{stats.holders:,}*\n\n"
        f"📌  Contract:\n`{cfg.contract_address}`\n\n"
        f"{cfg.branding}"
    )


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    tg_user = update.effective_user
    if not tg_user:
        return
    username = tg_user.username or tg_user.first_name or "anon"

    referral_code: str | None = None
    if ctx.args and ctx.args[0].startswith("ref_"):
        referral_code = ctx.args[0][4:]

    is_new = False
    with get_session() as session:
        existing = _get_user(session, tg_user.id)
        if not existing:
            is_new = True
            address, private_key, mnemonic = create_wallet()
            encrypted_key = encrypt_secret(private_key, cfg.encryption_key)
            encrypted_seed = encrypt_secret(mnemonic, cfg.encryption_key)

            user = User(
                telegram_id=tg_user.id,
                username=username,
                wallet_address=address,
                encrypted_private_key=encrypted_key,
                referral_code=make_referral_code(tg_user.id),
            )

            referrer = None
            if referral_code:
                referrer = session.scalar(select(User).where(User.referral_code == referral_code))
                if referrer and referrer.telegram_id != tg_user.id:
                    user.referred_by = referrer.telegram_id
                    session.add(Referral(
                        referrer_id=referrer.telegram_id,
                        referee_id=tg_user.id,
                        reward_amount=cfg.referral_bonus_amount,
                    ))

            session.add(user)
            session.commit()

            referrer_name = referrer.username if referrer else None

            # DM sensitive info
            try:
                await ctx.bot.send_message(
                    tg_user.id,
                    f"🔐 *Your Error404 Wallet is Ready!*\n"
                    f"{DIVIDER}\n\n"
                    f"👛 *Address:*\n`{address}`\n\n"
                    f"🔑 *Encrypted Private Key:*\n`{encrypted_key}`\n\n"
                    f"🌱 *Encrypted Seed Phrase:*\n`{encrypted_seed}`\n\n"
                    f"⚠️ *IMPORTANT:* Back these up securely offline.\n"
                    f"Never share your seed or private key with anyone.\n\n"
                    f"{cfg.branding}\n\n{DISCLAIMER}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as exc:
                log.warning("DM failed for %s: %s", tg_user.id, exc)

            # Admin notifications
            await notify_new_user(ctx.bot, cfg, username, tg_user.id, address, referrer_name)
            await notify_wallet_created(ctx.bot, cfg, username, tg_user.id, address)

            if referrer and referrer_name:
                await notify_referral(ctx.bot, cfg, referrer_name, username)
                try:
                    await ctx.bot.send_message(
                        referrer.telegram_id,
                        f"🎉 *Referral Successful!*\n\n"
                        f"*@{username}* joined using your referral link!\n"
                        f"💰 Bonus: *{cfg.referral_bonus_amount} {cfg.token_symbol}* "
                        f"will be credited to your wallet.\n\n"
                        f"{cfg.branding}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            # Group announcement
            try:
                await ctx.bot.send_message(
                    cfg.group_chat_id,
                    f"🚀 *@{username}* just joined Error404!\n"
                    f"Wallet: `{address}`\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

    text = await _build_welcome(cfg, _chain(ctx))
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_welcome(),
        disable_web_page_preview=True,
    )


# ── /wallet ───────────────────────────────────────────────────────────────────

async def wallet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_wallet(update.effective_message, ctx, update.effective_user.id)


async def _show_wallet(message, ctx, uid: int) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    with get_session() as session:
        user = _get_user(session, uid)

    if not user:
        # No wallet yet — show creation/import menu
        await message.reply_text(
            f"👛 *Wallet*\n{DIVIDER}\n\n"
            f"You don't have a wallet yet.\n\n"
            f"Choose an option below to get started:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_wallet_home(),
        )
        return

    eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
    tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    tok_usd = tok_bal * stats.price_usd
    eth_usd = eth_bal * 3000  # approximate

    with get_session() as session:
        recent = session.scalars(
            select(Transaction)
            .where(Transaction.wallet_address == user.wallet_address)
            .order_by(Transaction.timestamp.desc()).limit(5)
        ).all()

    tx_lines = "\n".join(
        f"  {'🟢' if t.tx_type in ('buy','receive') else '🔴'} "
        f"`{t.tx_type.upper()}` {t.amount:.4f} {t.token_symbol} "
        f"— [tx]({cfg.tx_link(t.tx_hash)})"
        for t in recent
    ) or "  _No transactions yet_"

    await message.reply_text(
        f"👛 *Your Wallet*\n{DIVIDER}\n\n"
        f"📌 Address:\n`{user.wallet_address}`\n\n"
        f"💎  ETH:      `{eth_bal:.6f}` (~${eth_usd:,.2f})\n"
        f"🪙  {cfg.token_symbol}:  `{tok_bal:,.4f}` (~${tok_usd:,.4f})\n\n"
        f"📋 *Recent Transactions:*\n{tx_lines}\n\n"
        f"[🔍 View on Explorer]({cfg.address_link(user.wallet_address)})",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=kb_wallet_view(has_wallet=True),
    )


# ── /contract ─────────────────────────────────────────────────────────────────

async def contract_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    await update.effective_message.reply_text(
        f"📜 *Error404 Contract Info*\n{DIVIDER}\n\n"
        f"🪙  Token: *Error404 ({cfg.token_symbol})*\n"
        f"📐  Standard: ERC-404 (hybrid ERC-20 / ERC-721)\n"
        f"⛓  Chain: Robinhood Chain (ID: {cfg.chain_id})\n\n"
        f"📌  Contract:\n`{cfg.contract_address}`\n\n"
        f"[🔍 View on Explorer]({cfg.address_link(cfg.contract_address)})\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=kb_back_main(),
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    await update.effective_message.reply_text(
        f"❓ *Error404 Bot — Help*\n{DIVIDER}\n\n"
        f"*Commands:*\n"
        f"  /start — Live stats + main menu\n"
        f"  /wallet — Wallet management\n"
        f"  /contract — Token contract info\n"
        f"  /alert above 0.001 — Price alert\n"
        f"  /alert below 0.0005 — Price alert\n"
        f"  /help — This menu\n\n"
        f"*Trading (via menu):*\n"
        f"  🟢 Buy · 🔴 Sell · 📈 DCA\n"
        f"  Limit Orders · Transfer · Portfolio\n\n"
        f"*Wallet (via menu):*\n"
        f"  Create · Import (key/seed) · Export\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_back_main(),
    )


# ── /alert ────────────────────────────────────────────────────────────────────

async def alert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    if len(args) != 2 or args[0] not in ("above", "below"):
        await update.effective_message.reply_text(
            "Usage:\n`/alert above 0.001`\n`/alert below 0.0005`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        price = float(args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid price.")
        return
    uid = update.effective_user.id
    with get_session() as session:
        if not _get_user(session, uid):
            await update.effective_message.reply_text("No wallet — send /start first.")
            return
        session.add(PriceAlert(user_id=uid, target_price=price, direction=args[0]))
        session.commit()
    await update.effective_message.reply_text(
        f"🔔 *Alert Set*\n\nYou'll be notified when {_cfg(ctx).token_symbol} goes "
        f"*{args[0]}* `${price:.8f}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Callback router ───────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    uid = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or "anon"

    # ── Menu ──────────────────────────────────────────────────────────────────
    if data == "menu:main":
        text = await _build_welcome(cfg, chain)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=kb_main(), disable_web_page_preview=True)

    elif data == "menu:wallet":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text(
                f"👛 *Wallet*\n{DIVIDER}\n\n"
                f"You don't have a wallet yet.\nChoose an option below:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_wallet_home(),
            )
            return
        eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        tok_usd = tok_bal * stats.price_usd
        eth_usd = eth_bal * 3000
        with get_session() as session:
            recent = session.scalars(
                select(Transaction).where(Transaction.wallet_address == user.wallet_address)
                .order_by(Transaction.timestamp.desc()).limit(5)
            ).all()
        tx_lines = "\n".join(
            f"  {'🟢' if t.tx_type in ('buy', 'receive') else '🔴'} "
            f"`{t.tx_type.upper()}` {t.amount:.4f} {t.token_symbol} "
            f"— [tx]({cfg.tx_link(t.tx_hash)})"
            for t in recent
        ) or "  _No transactions yet_"
        await query.edit_message_text(
            f"👛 *Your Wallet*\n{DIVIDER}\n\n"
            f"📌 Address:\n`{user.wallet_address}`\n\n"
            f"💎  ETH:     `{eth_bal:.6f}` (~${eth_usd:,.2f})\n"
            f"🪙  {cfg.token_symbol}: `{tok_bal:,.4f}` (~${tok_usd:,.4f})\n\n"
            f"📋 *Recent Transactions:*\n{tx_lines}\n\n"
            f"[🔍 Explorer]({cfg.address_link(user.wallet_address)})",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=kb_wallet_view(has_wallet=True),
        )

    elif data == "menu:portfolio":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
            return
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        tok_usd = tok_bal * stats.price_usd
        eth_usd = eth_bal * 3000
        total = tok_usd + eth_usd
        icon = "🟢" if stats.change_24h >= 0 else "🔴"
        await query.edit_message_text(
            f"📊 *Portfolio*\n{DIVIDER}\n\n"
            f"💎  ETH:            `{eth_bal:.6f}` ≈ *${eth_usd:,.2f}*\n"
            f"🪙  {cfg.token_symbol}:         `{tok_bal:,.4f}` ≈ *${tok_usd:,.4f}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼  Total Value:    *${total:,.4f}*\n\n"
            f"💰  {cfg.token_symbol} Price: `${stats.price_usd:.8f}`\n"
            f"📈  24h Change:     {icon} *{stats.change_24h:+.2f}%*\n",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:top10":
        holders = await fetch_top_holders(cfg.contract_address, cfg.explorer_url, 10)
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = "\n".join(
            f"  {medals[i]} `{short_addr(h['address'])}` — *{h['balance']:,.2f}* {cfg.token_symbol}"
            for i, h in enumerate(holders)
        ) or "  _Could not fetch holders_"
        await query.edit_message_text(
            f"🐋 *Top 10 Holders*\n{DIVIDER}\n\n{lines}\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:monitor":
        with get_session() as session:
            user = _get_user(session, uid)
            if not user:
                await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
                return
            user.monitor_enabled = not user.monitor_enabled
            session.commit()
            state = "ON 🟢" if user.monitor_enabled else "OFF 🔴"
        await query.edit_message_text(
            f"🔔 *Wallet Monitor: {state}*\n{DIVIDER}\n\n"
            f"When enabled, every transaction on your wallet sends you a private DM alert.\n"
            f"Whale transactions are also flagged automatically.\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:refer":
        with get_session() as session:
            user = _get_user(session, uid)
            if not user:
                await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
                return
            total_refs = session.scalar(
                select(func.count(Referral.id)).where(Referral.referrer_id == uid)
            ) or 0
            total_bonus = session.scalar(
                select(func.sum(Referral.reward_amount)).where(Referral.referrer_id == uid)
            ) or 0.0
            code = user.referral_code

        bot_username = ctx.bot.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{code}"

        await query.edit_message_text(
            f"🤝 *Referral Program*\n{DIVIDER}\n\n"
            f"Earn *{cfg.referral_bonus_amount} {cfg.token_symbol}* for every friend you bring!\n\n"
            f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n"
            f"📊 *Your Stats:*\n"
            f"  👥  Total Referrals:  *{total_refs}*\n"
            f"  💰  Total Bonus:      *{total_bonus:,.2f} {cfg.token_symbol}*\n\n"
            f"📤 Share your link — your friends get a free wallet instantly!\n\n"
            f"{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb_refer(ref_link),
        )

    elif data == "refer:copy":
        with get_session() as session:
            user = _get_user(session, uid)
            code = user.referral_code if user else "N/A"
        ref_link = f"https://t.me/{ctx.bot.username}?start=ref_{code}"
        await query.answer(f"Link: {ref_link}", show_alert=True)

    elif data == "menu:settings":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
            return
        await query.edit_message_text(
            f"⚙️ *Settings*\n{DIVIDER}\n\nCustomise your trading experience:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user),
        )

    elif data == "menu:help":
        await query.edit_message_text(
            f"❓ *Error404 Bot — Help*\n{DIVIDER}\n\n"
            f"/start — Live stats + main menu\n"
            f"/wallet — Wallet management\n"
            f"/contract — Token contract info\n"
            f"/alert above|below PRICE\n"
            f"/help — This menu\n\n"
            f"{cfg.branding}\n\n{DISCLAIMER}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back_main(),
        )

    # ── Wallet flows ──────────────────────────────────────────────────────────
    elif data == "wallet:create":
        with get_session() as session:
            existing = _get_user(session, uid)
        if existing:
            await query.edit_message_text(
                f"⚠️ *Wallet Already Exists*\n{DIVIDER}\n\n"
                f"You already have a wallet:\n`{existing.wallet_address}`\n\n"
                f"To replace it, use the Replace option (your current wallet access will be lost).",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_wallet_view(has_wallet=True),
            )
            return
        await _do_create_wallet(query, ctx, uid, username, cfg)

    elif data == "wallet:replace":
        await query.edit_message_text(
            f"⚠️ *Replace Wallet?*\n{DIVIDER}\n\n"
            f"This will *permanently replace* your current wallet in the bot.\n\n"
            f"Make sure you have exported your private key or seed phrase first!\n\n"
            f"Your old wallet still exists on-chain — you just won't be able to "
            f"use it through this bot anymore.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_replace_confirm(),
        )

    elif data == "wallet:replace_confirm":
        await _do_create_wallet(query, ctx, uid, username, cfg, replace=True)

    elif data == "wallet:import_key":
        ctx.user_data["awaiting"] = "import_key"
        await query.edit_message_text(
            f"🔑 *Import via Private Key*\n{DIVIDER}\n\n"
            f"Send your *private key* in the next message.\n\n"
            f"⚠️ This message will be deleted immediately after processing.\n"
            f"Never share your key in public chats.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "wallet:import_seed":
        ctx.user_data["awaiting"] = "import_seed"
        await query.edit_message_text(
            f"🌱 *Import via Seed Phrase*\n{DIVIDER}\n\n"
            f"Send your *12 or 24 word seed phrase* in the next message.\n\n"
            f"⚠️ This message will be deleted immediately after processing.\n"
            f"Never share your seed phrase in public chats.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "wallet:export_key":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet found.", reply_markup=kb_back_main())
            return
        try:
            pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
            await ctx.bot.send_message(
                uid,
                f"🔑 *Your Private Key*\n{DIVIDER}\n\n"
                f"`{pk}`\n\n"
                f"⚠️ *KEEP THIS SECRET.* Anyone with this key controls your wallet.\n"
                f"Delete this message after saving it securely.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await query.answer("✅ Private key sent to your DM", show_alert=True)
        except Exception as exc:
            log.error("Export key failed: %s", exc)
            await query.answer("❌ Could not export key. Try again.", show_alert=True)

    elif data == "wallet:export_seed":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet found.", reply_markup=kb_back_main())
            return
        # Check if we have an encrypted seed stored (from create_wallet flow)
        # We store seed as encrypted_private_key field with a prefix marker in some setups
        # For simplicity we note that seed is only available if wallet was created here
        await ctx.bot.send_message(
            uid,
            f"🌱 *Seed Phrase Export*\n{DIVIDER}\n\n"
            f"Your seed phrase was sent to you when you first created your wallet.\n"
            f"Check your DM history with this bot for the original backup message.\n\n"
            f"If you imported your wallet, the seed phrase from the original "
            f"wallet source is the one to use.\n\n"
            f"⚠️ Never share your seed phrase with anyone.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer("ℹ️ Seed info sent to your DM", show_alert=True)

    # ── Trading ───────────────────────────────────────────────────────────────
    elif data == "trade:buy":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
            return
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        icon = "🟢" if stats.change_24h >= 0 else "🔴"
        await query.edit_message_text(
            f"🟢 *Buy {cfg.token_symbol}*\n{DIVIDER}\n\n"
            f"💰  Price:      *${stats.price_usd:.8f}*\n"
            f"📈  24h:        {icon} *{stats.change_24h:+.2f}%*\n"
            f"📐  Slippage:   *{user.slippage}%*\n"
            f"⛽  Gas:        *{user.gas_strategy.title()}*\n\n"
            f"Select ETH amount to spend:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_buy_amounts(user.default_eth_amount),
        )

    elif data.startswith("buy_preset:"):
        eth_amt = float(data.split(":")[1])
        _pending_buy[uid] = eth_amt
        await _show_buy_confirm(query, ctx, uid, eth_amt)

    elif data == "buy_custom":
        ctx.user_data["awaiting"] = "buy_amount"
        await query.edit_message_text(
            f"✏️ *Custom Buy Amount*\n{DIVIDER}\n\n"
            f"Enter ETH amount to spend (e.g. `0.05`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "buy_confirm:confirm":
        await _execute_buy(query, ctx, uid, username)

    elif data == "buy_confirm:cancel":
        _pending_buy.pop(uid, None)
        await query.edit_message_text("✖ Buy cancelled.", reply_markup=kb_back_main())

    elif data == "trade:sell":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
            return
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        if tok_bal <= 0:
            await query.edit_message_text(
                f"❌ You have no {cfg.token_symbol} to sell.", reply_markup=kb_back_main()
            )
            return
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        ctx.user_data["sell_balance"] = tok_bal
        await query.edit_message_text(
            f"🔴 *Sell {cfg.token_symbol}*\n{DIVIDER}\n\n"
            f"💰  Price:     *${stats.price_usd:.8f}*\n"
            f"🪙  Balance:   *{tok_bal:,.4f} {cfg.token_symbol}*\n"
            f"📐  Slippage:  *{user.slippage}%*\n\n"
            f"Select percentage to sell:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_sell_pct(),
        )

    elif data.startswith("sell_pct:"):
        pct = float(data.split(":")[1]) / 100
        amount = ctx.user_data.get("sell_balance", 0.0) * pct
        _pending_sell[uid] = amount
        await _show_sell_confirm(query, ctx, uid, amount)

    elif data == "sell_custom":
        ctx.user_data["awaiting"] = "sell_amount"
        await query.edit_message_text(
            f"✏️ *Custom Sell Amount*\n{DIVIDER}\n\n"
            f"Enter {cfg.token_symbol} amount to sell:",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "sell_confirm:confirm":
        await _execute_sell(query, ctx, uid, username)

    elif data == "sell_confirm:cancel":
        _pending_sell.pop(uid, None)
        await query.edit_message_text("✖ Sell cancelled.", reply_markup=kb_back_main())

    elif data == "trade:transfer":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.", reply_markup=kb_back_main())
            return
        ctx.user_data["awaiting"] = "transfer_to"
        await query.edit_message_text(
            f"↗️ *Transfer {cfg.token_symbol}*\n{DIVIDER}\n\n"
            f"Enter the *recipient wallet address* (0x...):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "transfer_confirm:confirm":
        await _execute_transfer(query, ctx, uid, username)

    elif data == "transfer_confirm:cancel":
        _pending_transfer_to.pop(uid, None)
        ctx.user_data.pop("transfer_amount", None)
        await query.edit_message_text("✖ Transfer cancelled.", reply_markup=kb_back_main())

    elif data == "trade:dca":
        ctx.user_data["awaiting"] = "dca_total"
        await query.edit_message_text(
            f"📈 *DCA — Dollar Cost Average*\n{DIVIDER}\n\n"
            f"Split your buy into multiple orders over time.\n\n"
            f"*Step 1/3:* Enter total ETH to spend (e.g. `0.5`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "trade:limit_buy":
        _pending_limit_type[uid] = "buy"
        ctx.user_data["awaiting"] = "limit_amount"
        await query.edit_message_text(
            f"📉 *Limit Buy*\n{DIVIDER}\n\n"
            f"Your order will execute automatically when the price drops to your target.\n\n"
            f"Enter ETH amount to spend (e.g. `0.1`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "trade:limit_sell":
        _pending_limit_type[uid] = "sell"
        ctx.user_data["awaiting"] = "limit_amount"
        await query.edit_message_text(
            f"📉 *Limit Sell*\n{DIVIDER}\n\n"
            f"Your order will execute automatically when the price rises to your target.\n\n"
            f"Enter {cfg.token_symbol} amount to sell:",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Settings ──────────────────────────────────────────────────────────────
    elif data == "settings:toggle_monitor":
        with get_session() as session:
            user = _get_user(session, uid)
            user.monitor_enabled = not user.monitor_enabled
            session.commit()
            await query.edit_message_text(
                f"⚙️ *Settings*\n{DIVIDER}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user),
            )

    elif data == "settings:toggle_broadcast":
        with get_session() as session:
            user = _get_user(session, uid)
            user.broadcast_enabled = not user.broadcast_enabled
            session.commit()
            await query.edit_message_text(
                f"⚙️ *Settings*\n{DIVIDER}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user),
            )

    elif data == "settings:cycle_gas":
        strategies = ["slow", "medium", "fast"]
        with get_session() as session:
            user = _get_user(session, uid)
            idx = strategies.index(user.gas_strategy) if user.gas_strategy in strategies else 1
            user.gas_strategy = strategies[(idx + 1) % 3]
            session.commit()
            await query.edit_message_text(
                f"⚙️ *Settings*\n{DIVIDER}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user),
            )

    elif data == "settings:slippage":
        ctx.user_data["awaiting"] = "slippage"
        await query.edit_message_text(
            f"📐 *Slippage Tolerance*\n{DIVIDER}\n\n"
            f"Enter slippage % (e.g. `1.5` for 1.5%):\nRecommended: 1–3%",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "settings:default_eth":
        ctx.user_data["awaiting"] = "default_eth"
        await query.edit_message_text(
            f"💰 *Default ETH Buy Amount*\n{DIVIDER}\n\nEnter amount in ETH (e.g. `0.05`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "settings:orders":
        with get_session() as session:
            orders = session.scalars(
                select(LimitOrder).where(LimitOrder.user_id == uid, LimitOrder.status == "pending")
            ).all()
        if not orders:
            await query.edit_message_text(
                f"📋 *Pending Limit Orders*\n{DIVIDER}\n\nYou have no pending orders.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
            )
        else:
            await query.edit_message_text(
                f"📋 *Pending Limit Orders*\n{DIVIDER}\n\nTap an order to cancel it:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_orders(orders),
            )

    elif data.startswith("cancel_order:"):
        order_id = int(data.split(":")[1])
        with get_session() as session:
            order = session.scalar(select(LimitOrder).where(LimitOrder.id == order_id))
            if order and order.user_id == uid:
                order.status = "cancelled"
                session.commit()
        await query.edit_message_text(
            "✅ Order cancelled.", reply_markup=kb_back_main()
        )


# ── Wallet creation helper ────────────────────────────────────────────────────

async def _do_create_wallet(query, ctx, uid: int, username: str,
                             cfg: Settings, replace: bool = False) -> None:
    address, private_key, mnemonic = create_wallet()
    encrypted_key = encrypt_secret(private_key, cfg.encryption_key)

    with get_session() as session:
        existing = _get_user(session, uid)
        if existing:
            if not replace:
                await query.edit_message_text(
                    "❌ You already have a wallet. Use Replace to overwrite.",
                    reply_markup=kb_wallet_view(has_wallet=True),
                )
                return
            existing.wallet_address = address
            existing.encrypted_private_key = encrypted_key
        else:
            session.add(User(
                telegram_id=uid, username=username,
                wallet_address=address, encrypted_private_key=encrypted_key,
                referral_code=make_referral_code(uid),
            ))
        session.commit()

    action = "Replaced" if replace else "Created"
    try:
        await ctx.bot.send_message(
            uid,
            f"🔐 *Wallet {action} Successfully!*\n{DIVIDER}\n\n"
            f"👛 *Address:*\n`{address}`\n\n"
            f"🔑 *Private Key:*\n`{private_key}`\n\n"
            f"🌱 *Seed Phrase:*\n`{mnemonic}`\n\n"
            f"⚠️ *Back these up securely and delete this message.*\n"
            f"Never share them with anyone.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.warning("Could not DM wallet info: %s", exc)

    await notify_wallet_created(ctx.bot, cfg, username, uid, address)
    await query.edit_message_text(
        f"✅ *Wallet {action}!*\n{DIVIDER}\n\n"
        f"📌 Address:\n`{address}`\n\n"
        f"🔑 Your private key and seed phrase have been sent to your DM.\n"
        f"[🔍 Explorer]({cfg.address_link(address)})",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=kb_wallet_view(has_wallet=True),
    )


# ── Text message handler ──────────────────────────────────────────────────────

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    uid = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or "anon"
    raw = update.effective_message.text or ""
    text = raw.strip()
    awaiting = ctx.user_data.get("awaiting")

    # Always delete messages containing private keys / seeds (in private chat)
    if update.effective_chat.type == "private" and awaiting in ("import_key", "import_seed"):
        try:
            await update.effective_message.delete()
        except Exception:
            pass

    if awaiting == "import_key":
        ctx.user_data.pop("awaiting", None)
        try:
            address, pk = import_from_private_key(text)
        except Exception:
            await update.effective_message.reply_text(
                "❌ Invalid private key. Please check and try again.",
                reply_markup=kb_back_main(),
            )
            return
        encrypted = encrypt_secret(pk, cfg.encryption_key)
        with get_session() as session:
            existing = _get_user(session, uid)
            if existing:
                existing.wallet_address = address
                existing.encrypted_private_key = encrypted
            else:
                session.add(User(
                    telegram_id=uid, username=username,
                    wallet_address=address, encrypted_private_key=encrypted,
                    referral_code=make_referral_code(uid),
                ))
            session.commit()
        await notify_wallet_imported(ctx.bot, cfg, username, uid, address, "Private Key")
        await update.effective_message.reply_text(
            f"✅ *Wallet Imported!*\n{DIVIDER}\n\n"
            f"📌 Address:\n`{address}`\n\n"
            f"[🔍 Explorer]({cfg.address_link(address)})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb_wallet_view(has_wallet=True),
        )
        return

    if awaiting == "import_seed":
        ctx.user_data.pop("awaiting", None)
        try:
            address, pk = import_from_mnemonic(text)
        except Exception:
            await update.effective_message.reply_text(
                "❌ Invalid seed phrase. Please check and try again.",
                reply_markup=kb_back_main(),
            )
            return
        encrypted = encrypt_secret(pk, cfg.encryption_key)
        with get_session() as session:
            existing = _get_user(session, uid)
            if existing:
                existing.wallet_address = address
                existing.encrypted_private_key = encrypted
            else:
                session.add(User(
                    telegram_id=uid, username=username,
                    wallet_address=address, encrypted_private_key=encrypted,
                    referral_code=make_referral_code(uid),
                ))
            session.commit()
        await notify_wallet_imported(ctx.bot, cfg, username, uid, address, "Seed Phrase")
        await update.effective_message.reply_text(
            f"✅ *Wallet Imported!*\n{DIVIDER}\n\n"
            f"📌 Address:\n`{address}`\n\n"
            f"[🔍 Explorer]({cfg.address_link(address)})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb_wallet_view(has_wallet=True),
        )
        return

    if awaiting == "buy_amount":
        try:
            eth_amt = float(text)
            assert eth_amt > 0
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter a positive number like `0.05`.")
            return
        ctx.user_data.pop("awaiting", None)
        _pending_buy[uid] = eth_amt
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        with get_session() as session:
            user = _get_user(session, uid)
            slip = user.slippage if user else 1.0
        expected = eth_amt / stats.price_eth if stats.price_eth > 0 else 0
        await update.effective_message.reply_text(
            f"🟢 *Confirm Buy*\n{DIVIDER}\n\n"
            f"  Spend:    `{eth_amt} ETH`\n"
            f"  Receive:  ~`{expected:,.4f} {cfg.token_symbol}`\n"
            f"  Slippage: `{slip}%`\n"
            f"  Price:    `${stats.price_usd:.8f}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm_cancel("buy_confirm"),
        )
        return

    if awaiting == "sell_amount":
        try:
            amount = float(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter a positive number.")
            return
        ctx.user_data.pop("awaiting", None)
        _pending_sell[uid] = amount
        await update.effective_message.reply_text(
            f"🔴 *Confirm Sell*\n{DIVIDER}\n\n"
            f"  Sell: `{amount:,.4f} {cfg.token_symbol}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm_cancel("sell_confirm"),
        )
        return

    if awaiting == "transfer_to":
        if not text.startswith("0x") or len(text) != 42:
            await update.effective_message.reply_text(
                "❌ Invalid address. Must be a 42-character hex address (0x...)."
            )
            return
        _pending_transfer_to[uid] = text
        ctx.user_data["awaiting"] = "transfer_amount"
        await update.effective_message.reply_text(
            f"↗️ *Transfer — Step 2/2*\n{DIVIDER}\n\n"
            f"To: `{text}`\n\nEnter {cfg.token_symbol} amount to send:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "transfer_amount":
        try:
            amount = float(text)
            assert amount > 0
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter a positive number.")
            return
        to_addr = _pending_transfer_to.get(uid, "")
        ctx.user_data["transfer_amount"] = amount
        ctx.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"↗️ *Confirm Transfer*\n{DIVIDER}\n\n"
            f"  Send:  `{amount:,.4f} {cfg.token_symbol}`\n"
            f"  To:    `{to_addr}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm_cancel("transfer_confirm"),
        )
        return

    if awaiting == "dca_total":
        try:
            ctx.user_data["dca_total"] = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter a number like `0.5`.")
            return
        ctx.user_data["awaiting"] = "dca_buys"
        await update.effective_message.reply_text(
            f"📈 *DCA — Step 2/3*\n{DIVIDER}\n\nHow many *individual buys*? (e.g. `5`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "dca_buys":
        try:
            ctx.user_data["dca_buys"] = int(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter a whole number.")
            return
        ctx.user_data["awaiting"] = "dca_interval"
        await update.effective_message.reply_text(
            f"📈 *DCA — Step 3/3*\n{DIVIDER}\n\nInterval in *minutes* between each buy? (e.g. `60`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "dca_interval":
        try:
            interval = int(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter whole minutes.")
            return
        import datetime as dt
        total = ctx.user_data.get("dca_total", 0)
        buys  = ctx.user_data.get("dca_buys", 1)
        eth_per = round(total / buys, 6)
        with get_session() as session:
            job = DCAJob(user_id=uid, total_eth=total, num_buys=buys,
                         interval_minutes=interval, eth_per_buy=eth_per,
                         next_run=dt.datetime.utcnow())
            session.add(job)
            session.commit()
            job_id = job.id
        ctx.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"✅ *DCA Scheduled!*\n{DIVIDER}\n\n"
            f"  Total:    `{total} ETH`\n"
            f"  Orders:   `{buys}` × `{eth_per} ETH`\n"
            f"  Interval: every `{interval}` minutes\n"
            f"  Job ID:   `{job_id}`\n\n"
            f"I'll notify you after each buy.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )
        return

    if awaiting == "limit_amount":
        try:
            _pending_limit_amount[uid] = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Invalid amount.")
            return
        ctx.user_data["awaiting"] = "limit_price"
        order_type = _pending_limit_type.get(uid, "buy")
        await update.effective_message.reply_text(
            f"📉 *Limit {order_type.title()} — Step 2/2*\n{DIVIDER}\n\n"
            f"Enter the *USD price target* to trigger (e.g. `0.000005`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if awaiting == "limit_price":
        try:
            price = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Invalid price.")
            return
        order_type = _pending_limit_type.pop(uid, "buy")
        amount = _pending_limit_amount.pop(uid, 0)
        with get_session() as session:
            session.add(LimitOrder(user_id=uid, order_type=order_type,
                                   token_address=cfg.contract_address,
                                   amount=amount, price_target=price))
            session.commit()
        ctx.user_data.pop("awaiting", None)
        unit = "ETH" if order_type == "buy" else cfg.token_symbol
        await update.effective_message.reply_text(
            f"✅ *Limit {order_type.title()} Set!*\n{DIVIDER}\n\n"
            f"  Amount:  `{amount} {unit}`\n"
            f"  Trigger: `${price:.8f}`\n\n"
            f"I'll execute automatically when the price hits your target.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )
        return

    if awaiting == "slippage":
        try:
            slip = float(text)
            assert 0.1 <= slip <= 50
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter a value between 0.1 and 50.")
            return
        with get_session() as session:
            user = _get_user(session, uid)
            user.slippage = slip
            session.commit()
        ctx.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"✅ Slippage set to *{slip}%*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )
        return

    if awaiting == "default_eth":
        try:
            amt = float(text)
            assert amt > 0
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Invalid amount.")
            return
        with get_session() as session:
            user = _get_user(session, uid)
            user.default_eth_amount = amt
            session.commit()
        ctx.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"✅ Default buy set to *{amt} ETH*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )
        return

    # Not in a conversation — moderate group messages
    await moderate(update, ctx)


# ── Trade execution helpers ───────────────────────────────────────────────────

async def _show_buy_confirm(query, ctx, uid: int, eth_amt: float) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    with get_session() as session:
        user = _get_user(session, uid)
        slip = user.slippage if user else 1.0
    expected = eth_amt / stats.price_eth if stats.price_eth > 0 else 0
    await query.edit_message_text(
        f"🟢 *Confirm Buy*\n{DIVIDER}\n\n"
        f"  Spend:    `{eth_amt} ETH`\n"
        f"  Receive:  ~`{expected:,.4f} {cfg.token_symbol}`\n"
        f"  Slippage: `{slip}%`\n"
        f"  Price:    `${stats.price_usd:.8f}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_confirm_cancel("buy_confirm"),
    )


async def _show_sell_confirm(query, ctx, uid: int, amount: float) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    with get_session() as session:
        user = _get_user(session, uid)
        slip = user.slippage if user else 1.0
    expected_eth = amount * stats.price_eth
    await query.edit_message_text(
        f"🔴 *Confirm Sell*\n{DIVIDER}\n\n"
        f"  Sell:     `{amount:,.4f} {cfg.token_symbol}`\n"
        f"  Receive:  ~`{expected_eth:.6f} ETH`\n"
        f"  Slippage: `{slip}%`\n"
        f"  Price:    `${stats.price_usd:.8f}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_confirm_cancel("sell_confirm"),
    )


async def _execute_buy(query, ctx, uid: int, username: str) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    eth_amt = _pending_buy.pop(uid, None)
    if eth_amt is None:
        await query.edit_message_text("❌ Session expired. Try again.", reply_markup=kb_back_main())
        return
    await query.edit_message_text("⏳ *Executing buy...*\n\nPlease wait.", parse_mode=ParseMode.MARKDOWN)
    with get_session() as session:
        user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet found.", reply_markup=kb_back_main())
            return
        pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
        addr, slip, gas, broadcast = user.wallet_address, user.slippage, user.gas_strategy, user.broadcast_enabled

    result = await chain.buy_error(addr, pk, eth_amt, slip, gas)
    if result.success:
        with get_session() as session:
            session.add(Transaction(
                wallet_address=addr, tx_hash=result.tx_hash, tx_type="buy",
                amount=result.amount_out, token_symbol=cfg.token_symbol,
                token_contract=cfg.contract_address, broadcasted=broadcast,
            ))
            session.commit()
        tx_link = cfg.tx_link(result.tx_hash)
        await query.edit_message_text(
            f"✅ *Buy Successful!*\n{DIVIDER}\n\n"
            f"  Spent:    `{eth_amt} ETH`\n"
            f"  Received: ~`{result.amount_out:,.4f} {cfg.token_symbol}`\n\n"
            f"[🔍 View Transaction]({tx_link})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=kb_back_main(),
        )
        await notify_trade(ctx.bot, cfg, username, "BUY",
                           f"{eth_amt} ETH → {result.amount_out:,.4f} {cfg.token_symbol}",
                           addr, result.tx_hash)
        if broadcast:
            try:
                await ctx.bot.send_message(
                    cfg.group_chat_id,
                    f"🟢 *@{username}* BOUGHT "
                    f"`{result.amount_out:,.4f} {cfg.token_symbol}` "
                    f"for `{eth_amt} ETH`\n[Tx]({tx_link})\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                )
            except Exception:
                pass
    else:
        await query.edit_message_text(
            f"❌ *Buy Failed*\n{DIVIDER}\n\n`{result.error}`\n\n"
            f"Check your ETH balance and try again.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )


async def _execute_sell(query, ctx, uid: int, username: str) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    token_amt = _pending_sell.pop(uid, None)
    if token_amt is None:
        await query.edit_message_text("❌ Session expired. Try again.", reply_markup=kb_back_main())
        return
    await query.edit_message_text("⏳ *Executing sell...*\n\nPlease wait.", parse_mode=ParseMode.MARKDOWN)
    with get_session() as session:
        user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet found.", reply_markup=kb_back_main())
            return
        pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
        addr, slip, gas, broadcast = user.wallet_address, user.slippage, user.gas_strategy, user.broadcast_enabled

    result = await chain.sell_error(addr, pk, token_amt, slip, gas)
    if result.success:
        with get_session() as session:
            session.add(Transaction(
                wallet_address=addr, tx_hash=result.tx_hash, tx_type="sell",
                amount=token_amt, token_symbol=cfg.token_symbol,
                token_contract=cfg.contract_address, broadcasted=broadcast,
            ))
            session.commit()
        tx_link = cfg.tx_link(result.tx_hash)
        await query.edit_message_text(
            f"✅ *Sell Successful!*\n{DIVIDER}\n\n"
            f"  Sold:     `{token_amt:,.4f} {cfg.token_symbol}`\n"
            f"  Received: ~`{result.amount_out:.6f} ETH`\n\n"
            f"[🔍 View Transaction]({tx_link})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=kb_back_main(),
        )
        await notify_trade(ctx.bot, cfg, username, "SELL",
                           f"{token_amt:,.4f} {cfg.token_symbol} → {result.amount_out:.6f} ETH",
                           addr, result.tx_hash)
        if broadcast:
            try:
                await ctx.bot.send_message(
                    cfg.group_chat_id,
                    f"🔴 *@{username}* SOLD "
                    f"`{token_amt:,.4f} {cfg.token_symbol}` "
                    f"→ `{result.amount_out:.6f} ETH`\n[Tx]({tx_link})\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                )
            except Exception:
                pass
    else:
        await query.edit_message_text(
            f"❌ *Sell Failed*\n{DIVIDER}\n\n`{result.error}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )


async def _execute_transfer(query, ctx, uid: int, username: str) -> None:
    cfg = _cfg(ctx)
    chain = _chain(ctx)
    to_addr = _pending_transfer_to.pop(uid, None)
    amount = ctx.user_data.pop("transfer_amount", None)
    if not to_addr or not amount:
        await query.edit_message_text("❌ Session expired. Try again.", reply_markup=kb_back_main())
        return
    await query.edit_message_text("⏳ *Sending transfer...*\n\nPlease wait.", parse_mode=ParseMode.MARKDOWN)
    with get_session() as session:
        user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet found.", reply_markup=kb_back_main())
            return
        pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
        addr, gas = user.wallet_address, user.gas_strategy

    result = await chain.send_token(addr, pk, to_addr, amount, gas)
    if result.success:
        tx_link = cfg.tx_link(result.tx_hash)
        with get_session() as session:
            session.add(Transaction(
                wallet_address=addr, tx_hash=result.tx_hash, tx_type="send",
                amount=amount, token_symbol=cfg.token_symbol, token_contract=cfg.contract_address,
            ))
            session.commit()
        await query.edit_message_text(
            f"✅ *Transfer Successful!*\n{DIVIDER}\n\n"
            f"  Sent:  `{amount:,.4f} {cfg.token_symbol}`\n"
            f"  To:    `{to_addr}`\n\n"
            f"[🔍 View Transaction]({tx_link})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=kb_back_main(),
        )
    else:
        await query.edit_message_text(
            f"❌ *Transfer Failed*\n{DIVIDER}\n\n`{result.error}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )


# ── Moderation ────────────────────────────────────────────────────────────────

async def moderate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    msg = update.effective_message
    if not msg or not msg.text or update.effective_chat.type == "private":
        return
    uid = update.effective_user.id
    text = msg.text.strip()
    reason = None
    if len(URL_RE.findall(text)) > MAX_URLS:
        reason = "too many links"
    _recent[uid].append(text.lower())
    if list(_recent[uid]).count(text.lower()) > REPEAT_LIMIT:
        reason = "repeated spam"
    found = ADDRESS_RE.findall(text)
    if found and any(a.lower() != cfg.contract_address.lower() for a in found):
        reason = "unverified contract address"
    if not reason:
        return
    try:
        await msg.delete()
    except Exception:
        pass
    _strikes[uid] += 1
    warn = (
        f"🚫 @{update.effective_user.username or uid} — 3 strikes ({reason}). Admins please review."
        if _strikes[uid] >= 3
        else f"⚠️ @{update.effective_user.username or uid} — message removed ({reason}). Strike {_strikes[uid]}/3."
    )
    try:
        await ctx.bot.send_message(cfg.group_chat_id, warn)
    except Exception:
        pass


async def welcome_new_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(ctx)
    for member in update.message.new_chat_members or []:
        if member.is_bot:
            continue
        try:
            await ctx.bot.send_message(
                member.id,
                f"👋 *Welcome to Error404, {member.first_name}!*\n{DIVIDER}\n\n"
                f"You've just joined the *{cfg.token_symbol}* community on Robinhood Chain.\n\n"
                f"Tap /start to get your free on-chain wallet and start trading!\n\n"
                f"🌐 [error404.world]({cfg.project_website})\n"
                f"🐦 [Twitter](https://x.com/erro404hood)\n"
                f"💬 [Telegram Group](https://t.me/error404groupofficial)\n\n"
                f"{DISCLAIMER}",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await update.message.reply_text(
                    f"👋 Welcome @{member.username or member.first_name}! "
                    f"DM me /start to claim your free wallet.\n{cfg.branding_plain}"
                )
            except Exception:
                pass


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", ctx.error)
