"""All inline keyboard builders."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ── Welcome / Main ────────────────────────────────────────────────────────────

def kb_welcome() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀  Launch App", callback_data="menu:main"),
    ]])


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢  Buy $ERROR",   callback_data="trade:buy"),
            InlineKeyboardButton("🔴  Sell $ERROR",  callback_data="trade:sell"),
        ],
        [
            InlineKeyboardButton("📈  DCA",           callback_data="trade:dca"),
            InlineKeyboardButton("📉  Limit Buy",     callback_data="trade:limit_buy"),
            InlineKeyboardButton("📉  Limit Sell",    callback_data="trade:limit_sell"),
        ],
        [
            InlineKeyboardButton("👛  Wallet",        callback_data="menu:wallet"),
            InlineKeyboardButton("📊  Portfolio",     callback_data="menu:portfolio"),
        ],
        [
            InlineKeyboardButton("↗️  Transfer",      callback_data="trade:transfer"),
            InlineKeyboardButton("🐋  Top 10",        callback_data="menu:top10"),
        ],
        [
            InlineKeyboardButton("🔔  Monitor",       callback_data="menu:monitor"),
            InlineKeyboardButton("🤝  Refer",         callback_data="menu:refer"),
        ],
        [
            InlineKeyboardButton("⚙️  Settings",      callback_data="menu:settings"),
            InlineKeyboardButton("❓  Help",          callback_data="menu:help"),
        ],
    ])


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("‹ Back to Menu", callback_data="menu:main"),
    ]])


def kb_confirm_cancel(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅  Confirm", callback_data=f"{prefix}:confirm"),
        InlineKeyboardButton("✖  Cancel",  callback_data=f"{prefix}:cancel"),
    ]])


# ── Wallet ────────────────────────────────────────────────────────────────────

def kb_wallet_home() -> InlineKeyboardMarkup:
    """Shown immediately when user taps Wallet."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕  Create Wallet",       callback_data="wallet:create"),
            InlineKeyboardButton("🔑  Import Private Key",  callback_data="wallet:import_key"),
        ],
        [
            InlineKeyboardButton("🌱  Import Seed Phrase",  callback_data="wallet:import_seed"),
        ],
        [
            InlineKeyboardButton("📤  Export Private Key",  callback_data="wallet:export_key"),
            InlineKeyboardButton("📋  Export Seed Phrase",  callback_data="wallet:export_seed"),
        ],
        [
            InlineKeyboardButton("‹ Back to Menu",          callback_data="menu:main"),
        ],
    ])


def kb_wallet_view(has_wallet: bool) -> InlineKeyboardMarkup:
    """Shown when user already has a wallet."""
    rows = []
    if has_wallet:
        rows.append([
            InlineKeyboardButton("📤  Export Key",    callback_data="wallet:export_key"),
            InlineKeyboardButton("🌱  Export Seed",   callback_data="wallet:export_seed"),
        ])
        rows.append([
            InlineKeyboardButton("🔄  Replace Wallet", callback_data="wallet:replace"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("➕  Create Wallet",      callback_data="wallet:create"),
            InlineKeyboardButton("🔑  Import Private Key", callback_data="wallet:import_key"),
        ])
        rows.append([
            InlineKeyboardButton("🌱  Import Seed Phrase", callback_data="wallet:import_seed"),
        ])
    rows.append([InlineKeyboardButton("‹ Back to Menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_replace_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️  Yes, Replace Wallet", callback_data="wallet:replace_confirm")],
        [InlineKeyboardButton("✖  Cancel",               callback_data="menu:wallet")],
    ])


def kb_import_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑  Private Key",    callback_data="wallet:import_key")],
        [InlineKeyboardButton("🌱  Seed Phrase",    callback_data="wallet:import_seed")],
        [InlineKeyboardButton("‹ Back",             callback_data="menu:wallet")],
    ])


# ── Trading ───────────────────────────────────────────────────────────────────

def kb_buy_amounts(default: float) -> InlineKeyboardMarkup:
    presets = [0.01, 0.05, 0.1, 0.5]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{v} ETH", callback_data=f"buy_preset:{v}") for v in presets],
        [InlineKeyboardButton("✏️  Custom Amount", callback_data="buy_custom")],
        [InlineKeyboardButton("‹ Back to Menu",    callback_data="menu:main")],
    ])


def kb_sell_pct() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p}%", callback_data=f"sell_pct:{p}") for p in [25, 50, 75, 100]],
        [InlineKeyboardButton("✏️  Custom Amount", callback_data="sell_custom")],
        [InlineKeyboardButton("‹ Back to Menu",    callback_data="menu:main")],
    ])


# ── Settings ──────────────────────────────────────────────────────────────────

def kb_settings(user) -> InlineKeyboardMarkup:
    mon = "ON 🟢" if user.monitor_enabled else "OFF 🔴"
    brd = "ON 🟢" if user.broadcast_enabled else "OFF 🔴"
    gas = user.gas_strategy.title()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔔  Monitor Alerts: {mon}",   callback_data="settings:toggle_monitor")],
        [InlineKeyboardButton(f"📢  Group Broadcast: {brd}",  callback_data="settings:toggle_broadcast")],
        [InlineKeyboardButton(f"⛽  Gas Speed: {gas}",        callback_data="settings:cycle_gas")],
        [InlineKeyboardButton("📐  Slippage Tolerance",        callback_data="settings:slippage")],
        [InlineKeyboardButton("💰  Default ETH Buy Amount",    callback_data="settings:default_eth")],
        [InlineKeyboardButton("📋  My Limit Orders",           callback_data="settings:orders")],
        [InlineKeyboardButton("‹ Back to Menu",                callback_data="menu:main")],
    ])


def kb_orders(orders: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"✖  #{o.id}  {'BUY' if o.order_type == 'buy' else 'SELL'}  "
            f"{o.amount} @ ${o.price_target:.8f}",
            callback_data=f"cancel_order:{o.id}",
        )]
        for o in orders
    ]
    rows.append([InlineKeyboardButton("‹ Back", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


# ── Referral ──────────────────────────────────────────────────────────────────

def kb_refer(ref_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤  Share Referral Link", switch_inline_query=ref_link)],
        [InlineKeyboardButton("📋  Copy Link",           callback_data="refer:copy")],
        [InlineKeyboardButton("‹ Back to Menu",          callback_data="menu:main")],
    ])
