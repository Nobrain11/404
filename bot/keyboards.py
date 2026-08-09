"""Inline keyboard builders for every screen."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def kb_welcome() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Continue →", callback_data="menu:main"),
    ]])


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Buy $ERROR",  callback_data="trade:buy"),
         InlineKeyboardButton("🔴 Sell $ERROR", callback_data="trade:sell")],
        [InlineKeyboardButton("📈 DCA",         callback_data="trade:dca"),
         InlineKeyboardButton("📉 Buy Limit",   callback_data="trade:limit_buy"),
         InlineKeyboardButton("📉 Sell Limit",  callback_data="trade:limit_sell")],
        [InlineKeyboardButton("👛 Wallet",      callback_data="menu:wallet"),
         InlineKeyboardButton("📊 Portfolio",   callback_data="menu:portfolio")],
        [InlineKeyboardButton("🐋 Top 10",      callback_data="menu:top10"),
         InlineKeyboardButton("🔔 Monitor",     callback_data="menu:monitor")],
        [InlineKeyboardButton("🤝 Refer",       callback_data="menu:refer"),
         InlineKeyboardButton("⚙️ Settings",    callback_data="menu:settings"),
         InlineKeyboardButton("❓ Help",        callback_data="menu:help")],
    ])


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu:main")]])


def kb_confirm_cancel(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"{prefix}:confirm"),
        InlineKeyboardButton("❌ Cancel",  callback_data=f"{prefix}:cancel"),
    ]])


def kb_sell_amount(balance: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p}%", callback_data=f"sell_pct:{p}") for p in [25, 50, 75, 100]],
        [InlineKeyboardButton("« Back", callback_data="menu:main")],
    ])


def kb_buy_amounts(default: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{v} ETH", callback_data=f"buy_preset:{v}") for v in [0.01, 0.05, 0.1, 0.5]],
        [InlineKeyboardButton("✏️ Enter custom amount", callback_data="buy_custom")],
        [InlineKeyboardButton("« Back", callback_data="menu:main")],
    ])


def kb_settings(user) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Monitor: " + ("ON" if user.monitor_enabled else "OFF"),   callback_data="settings:toggle_monitor")],
        [InlineKeyboardButton("📢 Broadcast: " + ("ON" if user.broadcast_enabled else "OFF"), callback_data="settings:toggle_broadcast")],
        [InlineKeyboardButton(f"⛽ Gas: {user.gas_strategy.title()}",                        callback_data="settings:cycle_gas")],
        [InlineKeyboardButton("📐 Set Slippage",                                             callback_data="settings:slippage")],
        [InlineKeyboardButton("💰 Set Default ETH Buy",                                      callback_data="settings:default_eth")],
        [InlineKeyboardButton("📋 My Limit Orders",                                          callback_data="settings:orders")],
        [InlineKeyboardButton("« Back",                                                      callback_data="menu:main")],
    ])


def kb_orders(orders: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"❌ #{o.id} {'BUY' if o.order_type == 'buy' else 'SELL'} {o.amount} @ ${o.price_target:.8f}",
            callback_data=f"cancel_order:{o.id}"
        )]
        for o in orders
    ]
    rows.append([InlineKeyboardButton("« Back", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)
