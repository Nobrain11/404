"""All command + callback handlers."""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque

from sqlalchemy import select, func
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .config import DISCLAIMER, Settings
from .crypto_utils import create_wallet, decrypt_secret, encrypt_secret, make_referral_code, short_addr
from .keyboards import (
    kb_back_main, kb_buy_amounts, kb_confirm_cancel,
    kb_main, kb_orders, kb_sell_amount, kb_settings, kb_welcome,
)
from .models import DCAJob, LimitOrder, PriceAlert, Referral, Transaction, User, get_session
from .price import fetch_market_stats, fetch_top_holders

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
MAX_URLS = 3
REPEAT_LIMIT = 3

_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))
_strikes: dict[int, int] = defaultdict(int)
_pending_buy: dict[int, float] = {}
_pending_sell: dict[int, float] = {}
_pending_limit_type: dict[int, str] = {}
_pending_limit_amount: dict[int, float] = {}


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _chain(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data.get("chain")


def _get_user(session, telegram_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


async def _welcome_text(cfg: Settings, chain=None) -> str:
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    icon = "🟢" if stats.change_24h >= 0 else "🔴"
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*ERROR 404 ({cfg.token_symbol}) — Live Stats*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Price: *${stats.price_usd:.8f}*\n"
        f"📈 24h: {icon} *{stats.change_24h:+.2f}%*\n"
        f"📊 Market Cap: *${stats.market_cap:,.2f}*\n"
        f"💧 Liquidity: *${stats.liquidity:,.2f}*\n"
        f"🔊 24h Volume: *${stats.volume_24h:,.2f}*\n"
        f"🟢 24h Buys: *{stats.buys_24h}*   🔴 Sells: *{stats.sells_24h}*\n"
        f"👥 Holders: *{stats.holders}*\n\n"
        f"📌 Contract:\n`{cfg.contract_address}`\n\n"
        f"{cfg.branding}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    tg_user = update.effective_user
    if not tg_user:
        return
    username = tg_user.username or tg_user.first_name or "anon"
    referral_code = None
    if context.args and context.args[0].startswith("ref_"):
        referral_code = context.args[0][4:]

    with get_session() as session:
        if not _get_user(session, tg_user.id):
            address, private_key = create_wallet()
            encrypted = encrypt_secret(private_key, cfg.encryption_key)
            user = User(
                telegram_id=tg_user.id, username=username,
                wallet_address=address, encrypted_private_key=encrypted,
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

            try:
                await context.bot.send_message(
                    tg_user.id,
                    f"🔐 *Your Error404 wallet is ready!*\n\n"
                    f"*Address:* `{address}`\n"
                    f"*Encrypted key (back this up!):*\n`{encrypted}`\n\n"
                    f"⚠️ Store offline. Losing it means losing funds.\n\n"
                    f"{cfg.branding}\n\n{DISCLAIMER}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as exc:
                log.warning("DM failed: %s", exc)

            if referrer:
                try:
                    await context.bot.send_message(
                        referrer.telegram_id,
                        f"🎉 *@{username}* joined via your referral!\n"
                        f"Bonus: *{cfg.referral_bonus_amount} {cfg.token_symbol}*\n\n{cfg.branding}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            try:
                await context.bot.send_message(
                    cfg.group_chat_id,
                    f"🚀 Welcome @{username}! Wallet: `{address}` – let's trade {cfg.token_symbol}!\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

    text = await _welcome_text(cfg, _chain(context))
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_welcome()
    )


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    with get_session() as session:
        user = _get_user(session, update.effective_user.id)
    if not user:
        await update.effective_message.reply_text("No wallet — send /start first.")
        return
    eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
    tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
    with get_session() as session:
        recent = session.scalars(
            select(Transaction).where(Transaction.wallet_address == user.wallet_address)
            .order_by(Transaction.timestamp.desc()).limit(5)
        ).all()
    tx_lines = "\n".join(
        f"• {t.tx_type.upper()} {t.amount:.4f} {t.token_symbol} — [tx]({cfg.tx_link(t.tx_hash)})"
        for t in recent
    ) or "_No transactions yet_"
    await update.effective_message.reply_text(
        f"👛 *Your Wallet*\n`{user.wallet_address}`\n\n"
        f"💎 ETH: `{eth_bal:.6f}`\n🪙 {cfg.token_symbol}: `{tok_bal:,.4f}`\n\n"
        f"*Recent Txs:*\n{tx_lines}\n\n"
        f"[Explorer]({cfg.address_link(user.wallet_address)})\n\n{cfg.branding}",
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
        reply_markup=kb_back_main(),
    )


async def contract_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    await update.effective_message.reply_text(
        f"📜 *Error404 ({cfg.token_symbol})* — ERC-404 on Robinhood Chain\n"
        f"`{cfg.contract_address}`\n"
        f"[Explorer]({cfg.address_link(cfg.contract_address)})\n\n{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    await update.effective_message.reply_text(
        "🤖 *Error404 Bot*\n\n"
        "/start — Live stats + wallet\n/wallet — Balances & txs\n"
        "/contract — Token info\n/alert above|below PRICE — Price alert\n/help — This menu\n\n"
        f"{cfg.branding}\n\n{DISCLAIMER}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 2 or args[0] not in ("above", "below"):
        await update.effective_message.reply_text(
            "Usage: `/alert above 0.001` or `/alert below 0.0005`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        price = float(args[1])
    except ValueError:
        await update.effective_message.reply_text("Invalid price.")
        return
    uid = update.effective_user.id
    with get_session() as session:
        if not _get_user(session, uid):
            await update.effective_message.reply_text("No wallet — send /start first.")
            return
        session.add(PriceAlert(user_id=uid, target_price=price, direction=args[0]))
        session.commit()
    await update.effective_message.reply_text(
        f"🔔 Alert set: notify when price goes *{args[0]}* `${price}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = _cfg(context)
    chain = _chain(context)
    uid = update.effective_user.id

    if data == "menu:main":
        text = await _welcome_text(cfg, chain)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

    elif data == "menu:wallet":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.")
            return
        eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        with get_session() as session:
            recent = session.scalars(
                select(Transaction).where(Transaction.wallet_address == user.wallet_address)
                .order_by(Transaction.timestamp.desc()).limit(5)
            ).all()
        tx_lines = "\n".join(
            f"• {t.tx_type.upper()} {t.amount:.4f} {t.token_symbol} — [tx]({cfg.tx_link(t.tx_hash)})"
            for t in recent
        ) or "_No transactions yet_"
        await query.edit_message_text(
            f"👛 *Your Wallet*\n`{user.wallet_address}`\n\n"
            f"💎 ETH: `{eth_bal:.6f}`\n🪙 {cfg.token_symbol}: `{tok_bal:,.4f}`\n\n"
            f"*Recent Txs:*\n{tx_lines}\n\n[Explorer]({cfg.address_link(user.wallet_address)})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=kb_back_main(),
        )

    elif data == "menu:portfolio":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.")
            return
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        eth_bal = await chain.native_balance(user.wallet_address) if chain else 0.0
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        tok_usd = tok_bal * stats.price_usd
        eth_usd = eth_bal * 3000
        await query.edit_message_text(
            f"📊 *Portfolio*\n\n"
            f"💎 ETH: `{eth_bal:.6f}` (~${eth_usd:,.2f})\n"
            f"🪙 {cfg.token_symbol}: `{tok_bal:,.4f}` (~${tok_usd:,.4f})\n\n"
            f"💼 *Total: ~${tok_usd + eth_usd:,.4f}*\n\n"
            f"{cfg.token_symbol} price: ${stats.price_usd:.8f}\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:top10":
        holders = await fetch_top_holders(cfg.contract_address, cfg.explorer_url, 10)
        lines = "\n".join(
            f"{i+1}. `{short_addr(h['address'])}` — {h['balance']:,.2f} {cfg.token_symbol}"
            for i, h in enumerate(holders)
        ) or "_Could not fetch_"
        await query.edit_message_text(
            f"🐋 *Top 10 Holders*\n\n{lines}\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:monitor":
        with get_session() as session:
            user = _get_user(session, uid)
            if not user:
                await query.edit_message_text("No wallet — send /start first.")
                return
            user.monitor_enabled = not user.monitor_enabled
            session.commit()
            state = "ON 🟢" if user.monitor_enabled else "OFF 🔴"
        await query.edit_message_text(
            f"🔔 *Wallet Monitor: {state}*\n\nEvery tx on your wallet will DM you.\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:refer":
        with get_session() as session:
            user = _get_user(session, uid)
            if not user:
                await query.edit_message_text("No wallet — send /start first.")
                return
            total_refs = session.scalar(
                select(func.count(Referral.id)).where(Referral.referrer_id == uid)
            ) or 0
            total_bonus = session.scalar(
                select(func.sum(Referral.reward_amount)).where(Referral.referrer_id == uid)
            ) or 0.0
            code = user.referral_code
        ref_link = f"https://t.me/{context.bot.username}?start=ref_{code}"
        await query.edit_message_text(
            f"🤝 *Referral Program*\n\n"
            f"Your link: `{ref_link}`\n\n"
            f"👥 Referrals: *{total_refs}*\n"
            f"💰 Bonus earned: *{total_bonus:.2f} {cfg.token_symbol}*\n\n"
            f"Earn *{cfg.referral_bonus_amount} {cfg.token_symbol}* per friend!\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "menu:settings":
        with get_session() as session:
            user = _get_user(session, uid)
        await query.edit_message_text(
            "⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user)
        )

    elif data == "menu:help":
        await query.edit_message_text(
            "🤖 *Error404 Bot Help*\n\n"
            "/start — Live stats\n/wallet — Balances\n/contract — Token info\n"
            "/alert above|below PRICE\n\n"
            f"{cfg.branding}\n\n{DISCLAIMER}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif data == "trade:buy":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.")
            return
        stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
        await query.edit_message_text(
            f"🟢 *Buy {cfg.token_symbol}*\n\nPrice: *${stats.price_usd:.8f}*\nSlippage: *{user.slippage}%*\n\nSelect ETH amount:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_buy_amounts(user.default_eth_amount),
        )

    elif data.startswith("buy_preset:"):
        eth_amt = float(data.split(":")[1])
        _pending_buy[uid] = eth_amt
        await _show_buy_confirm(query, context, uid, eth_amt)

    elif data == "buy_custom":
        context.user_data["awaiting"] = "buy_amount"
        await query.edit_message_text("✏️ Enter ETH amount (e.g. `0.05`):", parse_mode=ParseMode.MARKDOWN)

    elif data == "buy_confirm:confirm":
        await _execute_buy(query, context, uid)

    elif data == "buy_confirm:cancel":
        _pending_buy.pop(uid, None)
        await query.edit_message_text("❌ Buy cancelled.", reply_markup=kb_back_main())

    elif data == "trade:sell":
        with get_session() as session:
            user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet — send /start first.")
            return
        tok_bal = await chain.token_balance(user.wallet_address) if chain else 0.0
        if tok_bal <= 0:
            await query.edit_message_text(f"❌ No {cfg.token_symbol} to sell.", reply_markup=kb_back_main())
            return
        context.user_data["sell_balance"] = tok_bal
        await query.edit_message_text(
            f"🔴 *Sell {cfg.token_symbol}*\n\nBalance: *{tok_bal:,.4f}*\n\nSelect %:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_sell_amount(tok_bal),
        )

    elif data.startswith("sell_pct:"):
        pct = float(data.split(":")[1]) / 100
        amount = context.user_data.get("sell_balance", 0.0) * pct
        _pending_sell[uid] = amount
        await _show_sell_confirm(query, context, uid, amount)

    elif data == "sell_confirm:confirm":
        await _execute_sell(query, context, uid)

    elif data == "sell_confirm:cancel":
        _pending_sell.pop(uid, None)
        await query.edit_message_text("❌ Sell cancelled.", reply_markup=kb_back_main())

    elif data == "trade:dca":
        context.user_data["awaiting"] = "dca_total"
        await query.edit_message_text(
            "📈 *DCA Setup*\n\nStep 1/3: Total ETH to spend (e.g. `0.5`):",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "trade:limit_buy":
        _pending_limit_type[uid] = "buy"
        context.user_data["awaiting"] = "limit_amount"
        await query.edit_message_text("📉 *Limit Buy*\n\nEnter ETH amount:", parse_mode=ParseMode.MARKDOWN)

    elif data == "trade:limit_sell":
        _pending_limit_type[uid] = "sell"
        context.user_data["awaiting"] = "limit_amount"
        await query.edit_message_text("📉 *Limit Sell*\n\nEnter token amount:", parse_mode=ParseMode.MARKDOWN)

    elif data == "settings:toggle_monitor":
        with get_session() as session:
            user = _get_user(session, uid)
            user.monitor_enabled = not user.monitor_enabled
            session.commit()
            await query.edit_message_text("⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user))

    elif data == "settings:toggle_broadcast":
        with get_session() as session:
            user = _get_user(session, uid)
            user.broadcast_enabled = not user.broadcast_enabled
            session.commit()
            await query.edit_message_text("⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user))

    elif data == "settings:cycle_gas":
        strategies = ["slow", "medium", "fast"]
        with get_session() as session:
            user = _get_user(session, uid)
            idx = strategies.index(user.gas_strategy) if user.gas_strategy in strategies else 1
            user.gas_strategy = strategies[(idx + 1) % 3]
            session.commit()
            await query.edit_message_text("⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_settings(user))

    elif data == "settings:slippage":
        context.user_data["awaiting"] = "slippage"
        await query.edit_message_text("📐 Enter slippage % (e.g. `1.5`):", parse_mode=ParseMode.MARKDOWN)

    elif data == "settings:default_eth":
        context.user_data["awaiting"] = "default_eth"
        await query.edit_message_text("💰 Enter default ETH buy amount (e.g. `0.05`):", parse_mode=ParseMode.MARKDOWN)

    elif data == "settings:orders":
        with get_session() as session:
            orders = session.scalars(
                select(LimitOrder).where(LimitOrder.user_id == uid, LimitOrder.status == "pending")
            ).all()
        if not orders:
            await query.edit_message_text("📋 No pending orders.", reply_markup=kb_back_main())
        else:
            await query.edit_message_text(
                "📋 *Pending Limit Orders*", parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_orders(orders),
            )

    elif data.startswith("cancel_order:"):
        order_id = int(data.split(":")[1])
        with get_session() as session:
            order = session.scalar(select(LimitOrder).where(LimitOrder.id == order_id))
            if order and order.user_id == uid:
                order.status = "cancelled"
                session.commit()
        await query.edit_message_text("✅ Order cancelled.", reply_markup=kb_back_main())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == "buy_amount":
        try:
            eth_amt = float(text)
            assert eth_amt > 0
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter a positive number.")
            return
        context.user_data.pop("awaiting", None)
        _pending_buy[uid] = eth_amt
        cfg2 = _cfg(context)
        stats = await fetch_market_stats(cfg2.contract_address, cfg2.explorer_url, chain)
        with get_session() as session:
            user = _get_user(session, uid)
            slip = user.slippage if user else 1.0
        expected = eth_amt / stats.price_eth if stats.price_eth > 0 else 0
        await update.effective_message.reply_text(
            f"🟢 *Confirm Buy*\n\nSpend: `{eth_amt} ETH`\nReceive: ~`{expected:,.4f} {cfg2.token_symbol}`\nSlippage: `{slip}%`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_cancel("buy_confirm"),
        )

    elif awaiting == "dca_total":
        try:
            context.user_data["dca_total"] = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter a number.")
            return
        context.user_data["awaiting"] = "dca_buys"
        await update.effective_message.reply_text("Step 2/3: How many *buys*?", parse_mode=ParseMode.MARKDOWN)

    elif awaiting == "dca_buys":
        try:
            context.user_data["dca_buys"] = int(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter a whole number.")
            return
        context.user_data["awaiting"] = "dca_interval"
        await update.effective_message.reply_text("Step 3/3: Interval in *minutes*?", parse_mode=ParseMode.MARKDOWN)

    elif awaiting == "dca_interval":
        try:
            interval = int(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Enter whole minutes.")
            return
        import datetime as dt
        total = context.user_data.get("dca_total", 0)
        buys  = context.user_data.get("dca_buys", 1)
        with get_session() as session:
            job = DCAJob(user_id=uid, total_eth=total, num_buys=buys,
                         interval_minutes=interval, eth_per_buy=total/buys,
                         next_run=dt.datetime.utcnow())
            session.add(job)
            session.commit()
            job_id = job.id
        context.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"✅ *DCA Scheduled!*\n\n`{total} ETH` × {buys} buys every {interval}min\nJob ID: `{job_id}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif awaiting == "limit_amount":
        try:
            _pending_limit_amount[uid] = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Invalid amount.")
            return
        context.user_data["awaiting"] = "limit_price"
        await update.effective_message.reply_text("Enter *USD price target* (e.g. `0.000005`):", parse_mode=ParseMode.MARKDOWN)

    elif awaiting == "limit_price":
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
        context.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(
            f"✅ *Limit {order_type.title()} set!*\n\nAmount: `{amount}`\nTrigger: `${price:.8f}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main(),
        )

    elif awaiting == "slippage":
        try:
            slip = float(text)
            assert 0.1 <= slip <= 50
        except (ValueError, AssertionError):
            await update.effective_message.reply_text("❌ Enter 0.1–50.")
            return
        with get_session() as session:
            user = _get_user(session, uid)
            user.slippage = slip
            session.commit()
        context.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(f"✅ Slippage set to *{slip}%*",
                                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main())

    elif awaiting == "default_eth":
        try:
            amt = float(text)
        except ValueError:
            await update.effective_message.reply_text("❌ Invalid.")
            return
        with get_session() as session:
            user = _get_user(session, uid)
            user.default_eth_amount = amt
            session.commit()
        context.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(f"✅ Default buy: *{amt} ETH*",
                                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main())
    else:
        await moderate(update, context)


async def _show_buy_confirm(query, context, uid: int, eth_amt: float) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    with get_session() as session:
        user = _get_user(session, uid)
        slip = user.slippage if user else 1.0
    expected = eth_amt / stats.price_eth if stats.price_eth > 0 else 0
    await query.edit_message_text(
        f"🟢 *Confirm Buy*\n\nSpend: `{eth_amt} ETH`\nReceive: ~`{expected:,.4f} {cfg.token_symbol}`\nSlippage: `{slip}%`\nPrice: `${stats.price_usd:.8f}`",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_cancel("buy_confirm"),
    )


async def _show_sell_confirm(query, context, uid: int, amount: float) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    stats = await fetch_market_stats(cfg.contract_address, cfg.explorer_url, chain)
    with get_session() as session:
        user = _get_user(session, uid)
        slip = user.slippage if user else 1.0
    expected_eth = amount * stats.price_eth
    await query.edit_message_text(
        f"🔴 *Confirm Sell*\n\nSell: `{amount:,.4f} {cfg.token_symbol}`\nReceive: ~`{expected_eth:.6f} ETH`\nSlippage: `{slip}%`\nPrice: `${stats.price_usd:.8f}`",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_cancel("sell_confirm"),
    )


async def _execute_buy(query, context, uid: int) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    eth_amt = _pending_buy.pop(uid, None)
    if eth_amt is None:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_back_main())
        return
    await query.edit_message_text("⏳ Executing buy...")
    with get_session() as session:
        user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet.", reply_markup=kb_back_main())
            return
        pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
        addr, slip, gas, broadcast = user.wallet_address, user.slippage, user.gas_strategy, user.broadcast_enabled

    result = await chain.buy_error(addr, pk, eth_amt, slip, gas)
    if result.success:
        with get_session() as session:
            session.add(Transaction(wallet_address=addr, tx_hash=result.tx_hash, tx_type="buy",
                                    amount=result.amount_out, token_symbol=cfg.token_symbol,
                                    token_contract=cfg.contract_address, broadcasted=broadcast))
            session.commit()
        await query.edit_message_text(
            f"✅ *Buy Successful!*\n\nSpent: `{eth_amt} ETH`\nReceived: ~`{result.amount_out:,.4f} {cfg.token_symbol}`\n[View Tx]({cfg.tx_link(result.tx_hash)})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=kb_back_main(),
        )
        if broadcast:
            try:
                await context.bot.send_message(
                    cfg.group_chat_id,
                    f"🟢 @{query.from_user.username or uid} BOUGHT `{result.amount_out:,.4f} {cfg.token_symbol}` for `{eth_amt} ETH` [Tx]({cfg.tx_link(result.tx_hash)})\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                )
            except Exception:
                pass
    else:
        await query.edit_message_text(f"❌ *Buy Failed*\n\n`{result.error}`",
                                       parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main())


async def _execute_sell(query, context, uid: int) -> None:
    cfg = _cfg(context)
    chain = _chain(context)
    token_amt = _pending_sell.pop(uid, None)
    if token_amt is None:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_back_main())
        return
    await query.edit_message_text("⏳ Executing sell...")
    with get_session() as session:
        user = _get_user(session, uid)
        if not user:
            await query.edit_message_text("No wallet.", reply_markup=kb_back_main())
            return
        pk = decrypt_secret(user.encrypted_private_key, cfg.encryption_key)
        addr, slip, gas, broadcast = user.wallet_address, user.slippage, user.gas_strategy, user.broadcast_enabled

    result = await chain.sell_error(addr, pk, token_amt, slip, gas)
    if result.success:
        with get_session() as session:
            session.add(Transaction(wallet_address=addr, tx_hash=result.tx_hash, tx_type="sell",
                                    amount=token_amt, token_symbol=cfg.token_symbol,
                                    token_contract=cfg.contract_address, broadcasted=broadcast))
            session.commit()
        await query.edit_message_text(
            f"✅ *Sell Successful!*\n\nSold: `{token_amt:,.4f} {cfg.token_symbol}`\nReceived: ~`{result.amount_out:.6f} ETH`\n[View Tx]({cfg.tx_link(result.tx_hash)})\n\n{cfg.branding}",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=kb_back_main(),
        )
        if broadcast:
            try:
                await context.bot.send_message(
                    cfg.group_chat_id,
                    f"🔴 @{query.from_user.username or uid} SOLD `{token_amt:,.4f} {cfg.token_symbol}` → `{result.amount_out:.6f} ETH` [Tx]({cfg.tx_link(result.tx_hash)})\n{cfg.branding}",
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                )
            except Exception:
                pass
    else:
        await query.edit_message_text(f"❌ *Sell Failed*\n\n`{result.error}`",
                                       parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back_main())


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
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
    try:
        await context.bot.send_message(
            cfg.group_chat_id,
            f"🚫 @{update.effective_user.username or uid} has 3 strikes ({reason}). Admins review."
            if _strikes[uid] >= 3
            else f"⚠️ @{update.effective_user.username or uid} message removed ({reason}) — strike {_strikes[uid]}/3."
        )
    except Exception:
        pass


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    for member in update.message.new_chat_members or []:
        if member.is_bot:
            continue
        try:
            await context.bot.send_message(
                member.id,
                f"👋 Welcome to Error404, *{member.first_name}*!\n\n"
                f"Send /start to get your free wallet and trade {cfg.token_symbol}.\n\n"
                f"🌐 {cfg.project_website}\n🐦 https://x.com/erro404hood\n"
                f"💬 https://t.me/error404groupofficial\n\n{DISCLAIMER}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await update.message.reply_text(
                    f"👋 Welcome @{member.username or member.first_name}! DM me /start to claim your wallet.\n{cfg.branding}"
                )
            except Exception:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)
