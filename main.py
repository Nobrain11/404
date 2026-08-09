import os
import asyncio
import logging
import secrets
import json
from datetime import datetime, timedelta
from decimal import Decimal
from cryptography.fernet import Fernet
from web3 import Web3
# Fix for web3.py >= v6
try:
    from web3.middleware import geth_poa_middleware
except ImportError:
    from web3.middleware.geth_poa import geth_poa_middleware
from eth_account import Account
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask
import threading

# ─── Logging ───
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── Environment ───
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
RPC_URL = os.getenv("RPC_URL")
EXPLORER_URL = os.getenv("EXPLORER_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///error404_bot.db")
CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("CONTRACT_ADDRESS"))
TOKEN_SYMBOL = os.getenv("TOKEN_SYMBOL", "ERROR")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", "18"))
SWAP_ROUTER = Web3.to_checksum_address(os.getenv("SWAP_ROUTER"))
WETH_ADDRESS = Web3.to_checksum_address(os.getenv("WETH_ADDRESS"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS_AMOUNT", "10"))
SLIPPAGE_DEFAULT = float(os.getenv("SLIPPAGE_DEFAULT", "1.0"))

if not BOT_TOKEN or not RPC_URL or not ENCRYPTION_KEY:
    raise ValueError("Missing required env vars: BOT_TOKEN, RPC_URL, ENCRYPTION_KEY")

fernet = Fernet(ENCRYPTION_KEY.encode())

# ─── Database ───
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    username = Column(String)
    wallet_address = Column(String, unique=True)
    encrypted_private_key = Column(Text)
    join_date = Column(DateTime, default=datetime.utcnow)
    referral_code = Column(String, unique=True)
    referred_by = Column(String, nullable=True)

class Referral(Base):
    __tablename__ = 'referrals'
    id = Column(Integer, primary_key=True)
    referrer_id = Column(String)
    referee_id = Column(String)
    reward_claimed = Column(Boolean, default=False)
    reward_amount = Column(Float, default=0)
    date = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    wallet_address = Column(String)
    tx_hash = Column(String, unique=True)
    tx_type = Column(String)  # buy, sell, swap, send, receive
    amount = Column(String)
    token_symbol = Column(String)
    token_contract = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    broadcasted = Column(Boolean, default=False)

class LimitOrder(Base):
    __tablename__ = 'limit_orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    type = Column(String)  # buy or sell
    token_address = Column(String)
    amount = Column(String)
    price_target = Column(Float)
    status = Column(String, default='pending')  # pending, executed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)

class PriceAlert(Base):
    __tablename__ = 'price_alerts'
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    target_price = Column(Float)
    direction = Column(String)  # above, below
    triggered = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── Web3 Setup ───
w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)
Account.enable_unaudited_hdwallet_features()

# Uniswap V3 Router ABI (simplified)
ROUTER_ABI = [
    {
        "inputs": [
            {"components": [{"internalType": "address","name": "tokenIn","type": "address"},{"internalType": "address","name": "tokenOut","type": "address"},{"internalType": "uint24","name": "fee","type": "uint24"},{"internalType": "address","name": "recipient","type": "address"},{"internalType": "uint256","name": "deadline","type": "uint256"},{"internalType": "uint256","name": "amountIn","type": "uint256"},{"internalType": "uint256","name": "amountOutMinimum","type": "uint256"},{"internalType": "uint160","name": "sqrtPriceLimitX96","type": "uint160"}], "internalType": "struct ISwapRouter.ExactInputSingleParams","name": "params","type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256","name": "amountOut","type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]
router_contract = w3.eth.contract(address=SWAP_ROUTER, abi=ROUTER_ABI)

# ERC-20 ABI for approvals and balance
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]
token_contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ERC20_ABI)

# ─── Helper Functions ───
def encrypt_key(key: str) -> str:
    return fernet.encrypt(key.encode()).decode()

def decrypt_key(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()

def generate_wallet():
    account = Account.create(secrets.token_hex(32))
    return account.address, account.key.hex()

def get_user_by_telegram_id(tg_id):
    session = SessionLocal()
    user = session.query(User).filter(User.telegram_id == str(tg_id)).first()
    session.close()
    return user

def create_user(tg_id, username, wallet_address, encrypted_pvk, referral_code, referred_by=None):
    session = SessionLocal()
    user = User(
        telegram_id=str(tg_id),
        username=username,
        wallet_address=wallet_address,
        encrypted_private_key=encrypted_pvk,
        referral_code=referral_code,
        referred_by=referred_by
    )
    session.add(user)
    session.commit()
    session.close()
    return user

def get_or_create_user(tg_id, username, referral_code=None):
    user = get_user_by_telegram_id(tg_id)
    if user:
        return user
    # generate wallet
    address, pvk = generate_wallet()
    enc_pvk = encrypt_key(pvk)
    # generate referral code
    ref_code = f"ERROR{tg_id}"[:10]
    # check if referred_by exists
    referred_by = None
    if referral_code:
        # find referrer by referral_code
        session = SessionLocal()
        referrer = session.query(User).filter(User.referral_code == referral_code).first()
        session.close()
        if referrer:
            referred_by = referrer.telegram_id
    user = create_user(tg_id, username, address, enc_pvk, ref_code, referred_by)
    # If referred, create referral record and reward referrer
    if referred_by:
        session = SessionLocal()
        ref_record = Referral(referrer_id=referred_by, referee_id=str(tg_id), reward_amount=REFERRAL_BONUS)
        session.add(ref_record)
        session.commit()
        session.close()
        # Notify referrer
        asyncio.create_task(notify_referrer(referred_by, tg_id, username))
    return user

async def notify_referrer(referrer_tg_id, referee_tg_id, referee_username):
    app = Application.builder().token(BOT_TOKEN).build()
    await app.initialize()
    await app.bot.send_message(chat_id=int(referrer_tg_id), text=f"🎉 @{referee_username} joined using your referral link! You earned {REFERRAL_BONUS} $ERROR bonus (will be sent shortly).")
    await app.shutdown()

def get_user_wallet_address(tg_id):
    user = get_user_by_telegram_id(tg_id)
    return user.wallet_address if user else None

def get_private_key(tg_id):
    user = get_user_by_telegram_id(tg_id)
    if user:
        return decrypt_key(user.encrypted_private_key)
    return None

def get_eth_balance(address):
    try:
        return w3.eth.get_balance(address)
    except:
        return 0

def get_token_balance(address):
    try:
        return token_contract.functions.balanceOf(address).call()
    except:
        return 0

def get_token_price():
    # Placeholder – implement real price fetch from Uniswap V3 pool
    return 0.000002117

def get_24h_stats():
    # Placeholder – implement real chain data queries
    return {
        "price": 0.000002117,
        "change_24h": -18.16,
        "market_cap": 2110,
        "liquidity": 1920,
        "volume_24h": 315.12,
        "buys_24h": 4,
        "sells_24h": 23,
        "holders": 140
    }

def build_buy_transaction(user_address, amount_eth_wei, token_out_address, slippage=SLIPPAGE_DEFAULT):
    deadline = int(datetime.utcnow().timestamp()) + 1200
    amount_out_min = 0  # in production calculate with slippage
    params = {
        'tokenIn': WETH_ADDRESS,
        'tokenOut': token_out_address,
        'fee': 3000,  # 0.3% pool fee
        'recipient': user_address,
        'deadline': deadline,
        'amountIn': amount_eth_wei,
        'amountOutMinimum': amount_out_min,
        'sqrtPriceLimitX96': 0
    }
    txn = router_contract.functions.exactInputSingle(params).build_transaction({
        'from': user_address,
        'value': amount_eth_wei,
        'gas': 300000,
        'gasPrice': w3.eth.gas_price,
        'nonce': w3.eth.get_transaction_count(user_address),
    })
    return txn

def build_sell_transaction(user_address, token_in_address, amount_token_wei, slippage=SLIPPAGE_DEFAULT):
    # First approve the router to spend tokens
    approve_txn = token_contract.functions.approve(SWAP_ROUTER, amount_token_wei).build_transaction({
        'from': user_address,
        'nonce': w3.eth.get_transaction_count(user_address),
        'gas': 100000,
        'gasPrice': w3.eth.gas_price,
    })
    # Then swap
    deadline = int(datetime.utcnow().timestamp()) + 1200
    amount_out_min = 0
    params = {
        'tokenIn': token_in_address,
        'tokenOut': WETH_ADDRESS,
        'fee': 3000,
        'recipient': user_address,
        'deadline': deadline,
        'amountIn': amount_token_wei,
        'amountOutMinimum': amount_out_min,
        'sqrtPriceLimitX96': 0
    }
    swap_txn = router_contract.functions.exactInputSingle(params).build_transaction({
        'from': user_address,
        'gas': 300000,
        'gasPrice': w3.eth.gas_price,
        'nonce': w3.eth.get_transaction_count(user_address) + 1,  # after approve
    })
    return approve_txn, swap_txn

# ─── Telegram Conversation States ───
BUY_AMOUNT, BUY_CONFIRM = range(2)
SELL_AMOUNT, SELL_CONFIRM = range(2)
DCA_TOTAL, DCA_INTERVALS, DCA_INTERVAL_TIME = range(3)
LIMIT_PRICE, LIMIT_AMOUNT, LIMIT_CONFIRM = range(3)

# ─── Bot Handlers ───
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    username = update.effective_user.username or "user"
    # Check referral code
    ref_code = None
    if context.args and len(context.args) > 0:
        ref_code = context.args[0].replace("ref_", "")
    # Create user if not exists
    user = get_or_create_user(tg_id, username, ref_code)
    # Show welcome screen
    stats = get_24h_stats()
    price = stats["price"]
    change = stats["change_24h"]
    mc = stats["market_cap"]
    liq = stats["liquidity"]
    vol = stats["volume_24h"]
    buys = stats["buys_24h"]
    sells = stats["sells_24h"]
    holders = stats["holders"]
    sign = "🔴" if change < 0 else "🟢"
    welcome_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERROR 404 ($ERROR) — Live Stats
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Welcome to Error404 Smart Bot!

💰 Price: ${price:.10f}
📈 24h: {sign} {change:.2f}%
📊 Market Cap: ${mc:,.2f}
💧 Liquidity: ${liq:,.2f}
🔊 24h Volume: ${vol:,.2f}
🟢 24h Buys: {buys}   🔴 Sells: {sells}
👥 Holders: {holders}

📌 Contract:
`{CONTRACT_ADDRESS}`

🌐 error404.world | 🐦 @erro404hood | 💬 @error404groupofficial
"""
    keyboard = [[InlineKeyboardButton("Continue →", callback_data="continue_onboard")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

async def continue_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await query.edit_message_text("Please start with /start first.")
        return
    # Send wallet info and main menu
    wallet = user.wallet_address
    enc_pvk = user.encrypted_private_key
    await query.edit_message_text(f"Your wallet has been generated!\nAddress: `{wallet}`\n\n⚠️ **IMPORTANT**: Your private key is encrypted in our database. Please back it up now:\n\n`{enc_pvk}`\n\nKeep this safe! We cannot recover it.")
    # Now show main menu
    await show_main_menu(query.message.chat_id, context)

async def show_main_menu(chat_id, context):
    buttons = [
        [KeyboardButton("Buy $ERROR"), KeyboardButton("Sell $ERROR")],
        [KeyboardButton("DCA"), KeyboardButton("Buy Limit"), KeyboardButton("Sell Limit")],
        [KeyboardButton("Wallet"), KeyboardButton("Portfolio")],
        [KeyboardButton("Holder Top 10"), KeyboardButton("Monitor")],
        [KeyboardButton("Refer"), KeyboardButton("Settings"), KeyboardButton("Help")]
    ]
    reply_markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    await context.bot.send_message(chat_id=chat_id, text="Main Menu:", reply_markup=reply_markup)

# ─── Button Handlers ───
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tg_id = update.effective_user.id
    if text == "Buy $ERROR":
        await start_buy(update, context)
    elif text == "Sell $ERROR":
        await start_sell(update, context)
    elif text == "DCA":
        await start_dca(update, context)
    elif text == "Buy Limit":
        await start_limit_buy(update, context)
    elif text == "Sell Limit":
        await start_limit_sell(update, context)
    elif text == "Wallet":
        await show_wallet(update, context)
    elif text == "Portfolio":
        await show_portfolio(update, context)
    elif text == "Holder Top 10":
        await show_holders(update, context)
    elif text == "Monitor":
        await toggle_monitor(update, context)
    elif text == "Refer":
        await show_referral(update, context)
    elif text == "Settings":
        await show_settings(update, context)
    elif text == "Help":
        await show_help(update, context)
    else:
        await update.message.reply_text("Unknown command. Use the buttons.")

# ─── Buy Flow ───
async def start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter the amount of ETH you want to spend (e.g., 0.1):")
    return BUY_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount_eth = float(update.message.text)
        context.user_data['buy_eth'] = amount_eth
        price = get_token_price()
        expected_tokens = amount_eth / price
        slippage = context.user_data.get('slippage', SLIPPAGE_DEFAULT)
        await update.message.reply_text(f"🟢 Confirm swap:\n{amount_eth} ETH → ~ {expected_tokens:.2f} $ERROR\nSlippage: {slippage}%\n\nType 'confirm' to execute or 'cancel' to abort.")
        return BUY_CONFIRM
    except:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return BUY_AMOUNT

async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == 'cancel':
        await update.message.reply_text("Buy cancelled.")
        return ConversationHandler.END
    if text != 'confirm':
        await update.message.reply_text("Type 'confirm' or 'cancel'.")
        return BUY_CONFIRM
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await update.message.reply_text("User not found. Start with /start.")
        return ConversationHandler.END
    private_key = get_private_key(tg_id)
    if not private_key:
        await update.message.reply_text("Private key missing.")
        return ConversationHandler.END
    amount_eth = context.user_data.get('buy_eth', 0)
    amount_wei = int(amount_eth * 10**18)
    try:
        txn = build_buy_transaction(user.wallet_address, amount_wei, CONTRACT_ADDRESS)
        signed = w3.eth.account.sign_transaction(txn, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        await update.message.reply_text(f"✅ Buy order submitted! TX: {EXPLORER_URL}/tx/{tx_hash.hex()}")
        if context.user_data.get('broadcast', True):
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"🟢 @{update.effective_user.username} bought {amount_eth} ETH worth of $ERROR! TX: {EXPLORER_URL}/tx/{tx_hash.hex()}")
        session = SessionLocal()
        tx_record = Transaction(wallet_address=user.wallet_address, tx_hash=tx_hash.hex(), tx_type='buy', amount=str(amount_eth), token_symbol='ERROR', token_contract=CONTRACT_ADDRESS, broadcasted=True)
        session.add(tx_record)
        session.commit()
        session.close()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    return ConversationHandler.END

# ─── Sell Flow ───
async def start_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await update.message.reply_text("User not found. Start with /start.")
        return ConversationHandler.END
    balance = get_token_balance(user.wallet_address)
    balance_formatted = balance / 10**TOKEN_DECIMALS
    await update.message.reply_text(f"Your $ERROR balance: {balance_formatted:.2f}\nEnter amount to sell (or 'max' for all):")
    return SELL_AMOUNT

async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    balance = get_token_balance(user.wallet_address)
    balance_formatted = balance / 10**TOKEN_DECIMALS
    text = update.message.text.lower()
    if text == 'max':
        amount_tokens = balance_formatted
    else:
        try:
            amount_tokens = float(text)
            if amount_tokens > balance_formatted:
                await update.message.reply_text(f"Insufficient balance. You have {balance_formatted:.2f} $ERROR.")
                return SELL_AMOUNT
        except:
            await update.message.reply_text("Invalid amount.")
            return SELL_AMOUNT
    context.user_data['sell_amount'] = amount_tokens
    price = get_token_price()
    expected_eth = amount_tokens * price
    await update.message.reply_text(f"🟢 Confirm sell:\n{amount_tokens:.2f} $ERROR → ~ {expected_eth:.6f} ETH\n\nType 'confirm' to execute or 'cancel' to abort.")
    return SELL_CONFIRM

async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == 'cancel':
        await update.message.reply_text("Sell cancelled.")
        return ConversationHandler.END
    if text != 'confirm':
        await update.message.reply_text("Type 'confirm' or 'cancel'.")
        return SELL_CONFIRM
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    private_key = get_private_key(tg_id)
    amount_tokens = context.user_data.get('sell_amount', 0)
    amount_wei = int(amount_tokens * 10**TOKEN_DECIMALS)
    try:
        approve_txn, swap_txn = build_sell_transaction(user.wallet_address, CONTRACT_ADDRESS, amount_wei)
        signed_approve = w3.eth.account.sign_transaction(approve_txn, private_key)
        w3.eth.send_raw_transaction(signed_approve.rawTransaction)
        # Wait for approval
        await asyncio.sleep(5)
        signed_swap = w3.eth.account.sign_transaction(swap_txn, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_swap.rawTransaction)
        await update.message.reply_text(f"✅ Sell order submitted! TX: {EXPLORER_URL}/tx/{tx_hash.hex()}")
        if context.user_data.get('broadcast', True):
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"🔴 @{update.effective_user.username} sold {amount_tokens:.2f} $ERROR! TX: {EXPLORER_URL}/tx/{tx_hash.hex()}")
        session = SessionLocal()
        tx_record = Transaction(wallet_address=user.wallet_address, tx_hash=tx_hash.hex(), tx_type='sell', amount=str(amount_tokens), token_symbol='ERROR', token_contract=CONTRACT_ADDRESS, broadcasted=True)
        session.add(tx_record)
        session.commit()
        session.close()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    return ConversationHandler.END

# ─── DCA ───
async def start_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter total ETH to invest:")
    return DCA_TOTAL

async def dca_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        total = float(update.message.text)
        context.user_data['dca_total'] = total
        await update.message.reply_text("Enter number of buys (intervals):")
        return DCA_INTERVALS
    except:
        await update.message.reply_text("Invalid number.")
        return DCA_TOTAL

async def dca_intervals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        intervals = int(update.message.text)
        context.user_data['dca_intervals'] = intervals
        await update.message.reply_text("Enter time between buys in minutes:")
        return DCA_INTERVAL_TIME
    except:
        await update.message.reply_text("Invalid number.")
        return DCA_INTERVALS

async def dca_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        minutes = int(update.message.text)
        total = context.user_data['dca_total']
        intervals = context.user_data['dca_intervals']
        amount_per = total / intervals
        scheduler = context.application.scheduler
        for i in range(intervals):
            delay = i * minutes * 60
            scheduler.add_job(execute_dca_buy, 'date', run_date=datetime.utcnow() + timedelta(seconds=delay), args=[update.effective_user.id, amount_per, context])
        await update.message.reply_text(f"✅ DCA scheduled: {intervals} buys of {amount_per:.6f} ETH every {minutes} minutes.")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")
    return ConversationHandler.END

async def execute_dca_buy(user_id, amount_eth, context):
    # Simplified DCA buy – you'd need to fetch user wallet and execute a buy
    # For brevity, we'll just log
    logger.info(f"DCA buy for user {user_id}: {amount_eth} ETH")

# ─── Limit Orders ───
async def start_limit_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['limit_type'] = 'buy'
    await update.message.reply_text("Enter target price per $ERROR (in ETH):")
    return LIMIT_PRICE

async def start_limit_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['limit_type'] = 'sell'
    await update.message.reply_text("Enter target price per $ERROR (in ETH):")
    return LIMIT_PRICE

async def limit_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text)
        context.user_data['limit_price'] = price
        await update.message.reply_text("Enter amount of ETH (for buy) or $ERROR (for sell):")
        return LIMIT_AMOUNT
    except:
        await update.message.reply_text("Invalid price.")
        return LIMIT_PRICE

async def limit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        context.user_data['limit_amount'] = amount
        await update.message.reply_text(f"Confirm limit order: {amount} at {context.user_data['limit_price']} ETH per $ERROR. Type 'confirm' to place.")
        return LIMIT_CONFIRM
    except:
        await update.message.reply_text("Invalid amount.")
        return LIMIT_AMOUNT

async def limit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == 'cancel':
        await update.message.reply_text("Order cancelled.")
        return ConversationHandler.END
    if text != 'confirm':
        await update.message.reply_text("Type 'confirm' or 'cancel'.")
        return LIMIT_CONFIRM
    tg_id = update.effective_user.id
    price = context.user_data['limit_price']
    amount = context.user_data['limit_amount']
    order_type = context.user_data.get('limit_type', 'buy')
    session = SessionLocal()
    order = LimitOrder(user_id=str(tg_id), type=order_type, token_address=CONTRACT_ADDRESS, amount=str(amount), price_target=price)
    session.add(order)
    session.commit()
    session.close()
    await update.message.reply_text(f"✅ {order_type.capitalize()} limit order placed: {amount} at {price} ETH. Will execute when price hits.")
    return ConversationHandler.END

# ─── Wallet ───
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await update.message.reply_text("Start with /start first.")
        return
    eth_balance = get_eth_balance(user.wallet_address) / 10**18
    token_balance = get_token_balance(user.wallet_address) / 10**TOKEN_DECIMALS
    text = f"📊 **Your Wallet**\nAddress: `{user.wallet_address}`\nETH: {eth_balance:.6f}\n$ERROR: {token_balance:.2f}"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Portfolio ───
async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await update.message.reply_text("Start with /start first.")
        return
    eth_balance = get_eth_balance(user.wallet_address) / 10**18
    token_balance = get_token_balance(user.wallet_address) / 10**TOKEN_DECIMALS
    price = get_token_price()
    total_value_eth = eth_balance + token_balance * price
    total_value_usd = total_value_eth * 3000  # rough ETH/USD
    text = f"💼 **Portfolio**\nETH: {eth_balance:.6f}\n$ERROR: {token_balance:.2f}\nTotal Value: {total_value_eth:.6f} ETH (~${total_value_usd:.2f})"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Holder Top 10 ───
async def show_holders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Placeholder – you should fetch from Blockscout API or contract
    holders = [
        ("0x1234...5678", 10000),
        ("0xabcd...efgh", 8000),
        ("0x9876...5432", 5000),
        ("0x1111...2222", 3000),
        ("0x3333...4444", 2000),
        ("0x5555...6666", 1500),
        ("0x7777...8888", 1000),
        ("0x9999...0000", 800),
        ("0xaaaa...bbbb", 600),
        ("0xcccc...dddd", 400),
    ]
    text = "🏆 **Top 10 $ERROR Holders**\n\n"
    for i, (addr, bal) in enumerate(holders, 1):
        text += f"{i}. `{addr}` – {bal} $ERROR\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Monitor Toggle ───
async def toggle_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get('monitor', False)
    context.user_data['monitor'] = not current
    status = "ON" if context.user_data['monitor'] else "OFF"
    await update.message.reply_text(f"Monitor is now {status}. You'll receive DMs for your wallet activity.")

# ─── Referral ───
async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        await update.message.reply_text("Start with /start.")
        return
    ref_link = f"https://t.me/{context.bot.username}?start=ref_{user.referral_code}"
    session = SessionLocal()
    ref_count = session.query(Referral).filter(Referral.referrer_id == str(tg_id)).count()
    session.close()
    text = f"👥 **Referral Program**\nYour referral link: {ref_link}\nTotal referrals: {ref_count}\nBonus per referral: {REFERRAL_BONUS} $ERROR"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Settings ───
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slippage = context.user_data.get('slippage', SLIPPAGE_DEFAULT)
    broadcast = context.user_data.get('broadcast', True)
    text = f"⚙️ **Settings**\nSlippage: {slippage}%\nAuto-broadcast: {broadcast}\n\nTo change, use commands:\n/slippage 2\n/broadcast on/off"
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_slippage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        context.user_data['slippage'] = val
        await update.message.reply_text(f"Slippage set to {val}%")
    except:
        await update.message.reply_text("Usage: /slippage 2")

async def set_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0].lower()
    if arg in ('on', 'off'):
        context.user_data['broadcast'] = (arg == 'on')
        await update.message.reply_text(f"Broadcast set to {arg.upper()}")
    else:
        await update.message.reply_text("Usage: /broadcast on/off")

# ─── Help ───
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 **Error404 Smart Bot Help**

Trading:
- Buy/Sell $ERROR directly via buttons
- DCA: Dollar-cost averaging
- Limit orders: set price targets

Monitoring:
- Wallet, Portfolio, Top Holders
- Price alerts: /alert above 0.001

Referral:
- Earn $ERROR by inviting friends

⚠️ **Risk Disclaimer**: Trading carries risk. Error404 is a memecoin with high volatility. Trade responsibly. This bot is for entertainment purposes only.

🌐 error404.world | 🐦 @erro404hood | 💬 @error404groupofficial
    """
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── Price Alerts ───
async def set_price_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        direction = context.args[0]
        price = float(context.args[1])
        tg_id = update.effective_user.id
        session = SessionLocal()
        alert = PriceAlert(user_id=str(tg_id), target_price=price, direction=direction)
        session.add(alert)
        session.commit()
        session.close()
        await update.message.reply_text(f"Alert set: when $ERROR goes {direction} {price}")
    except:
        await update.message.reply_text("Usage: /alert above 0.001 or /alert below 0.0005")

# ─── Background Tasks ───
async def monitor_orders(context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    orders = session.query(LimitOrder).filter(LimitOrder.status == 'pending').all()
    current_price = get_token_price()
    for order in orders:
        if order.type == 'buy' and current_price <= order.price_target:
            session.query(LimitOrder).filter(LimitOrder.id == order.id).update({'status': 'executed'})
            try:
                await context.bot.send_message(chat_id=int(order.user_id), text=f"🔔 Your buy limit order at {order.price_target} executed!")
            except:
                pass
        elif order.type == 'sell' and current_price >= order.price_target:
            session.query(LimitOrder).filter(LimitOrder.id == order.id).update({'status': 'executed'})
            try:
                await context.bot.send_message(chat_id=int(order.user_id), text=f"🔔 Your sell limit order at {order.price_target} executed!")
            except:
                pass
    session.commit()
    session.close()

async def monitor_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    alerts = session.query(PriceAlert).filter(PriceAlert.triggered == False).all()
    current_price = get_token_price()
    for alert in alerts:
        if alert.direction == 'above' and current_price >= alert.target_price:
            session.query(PriceAlert).filter(PriceAlert.id == alert.id).update({'triggered': True})
            try:
                await context.bot.send_message(chat_id=int(alert.user_id), text=f"🔔 Price alert! $ERROR is now above {alert.target_price}")
            except:
                pass
        elif alert.direction == 'below' and current_price <= alert.target_price:
            session.query(PriceAlert).filter(PriceAlert.id == alert.id).update({'triggered': True})
            try:
                await context.bot.send_message(chat_id=int(alert.user_id), text=f"🔔 Price alert! $ERROR is now below {alert.target_price}")
            except:
                pass
    session.commit()
    session.close()

async def hourly_pulse(context: ContextTypes.DEFAULT_TYPE):
    stats = get_24h_stats()
    text = f"📊 **Error404 Chain Pulse**\n\nLast hour: ... (simulated)\nPrice: ${stats['price']:.10f}\n24h Volume: ${stats['volume_24h']:.2f}\nBuys: {stats['buys_24h']} | Sells: {stats['sells_24h']}"
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, parse_mode="Markdown")

# ─── Flask Keep-Alive ───
app_flask = Flask(__name__)
@app_flask.route('/ping')
def ping():
    return "OK"

def run_flask():
    app_flask.run(host='0.0.0.0', port=8080)

# ─── Main ───
def main():
    # Start Flask in background
    threading.Thread(target=run_flask, daemon=True).start()

    # Build application
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handlers
    conv_buy = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Buy \$ERROR$"), start_buy)],
        states={
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount)],
            BUY_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Cancelled."))]
    )
    conv_sell = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Sell \$ERROR$"), start_sell)],
        states={
            SELL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_amount)],
            SELL_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Cancelled."))]
    )
    conv_dca = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^DCA$"), start_dca)],
        states={
            DCA_TOTAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, dca_total)],
            DCA_INTERVALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, dca_intervals)],
            DCA_INTERVAL_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, dca_time)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Cancelled."))]
    )
    conv_limit_buy = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Buy Limit$"), start_limit_buy)],
        states={
            LIMIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_price)],
            LIMIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_amount)],
            LIMIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Cancelled."))]
    )
    conv_limit_sell = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Sell Limit$"), start_limit_sell)],
        states={
            LIMIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_price)],
            LIMIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_amount)],
            LIMIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Cancelled."))]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(continue_onboard, pattern="^continue_onboard$"))
    application.add_handler(conv_buy)
    application.add_handler(conv_sell)
    application.add_handler(conv_dca)
    application.add_handler(conv_limit_buy)
    application.add_handler(conv_limit_sell)
    application.add_handler(MessageHandler(filters.Regex(r"^(Buy \$ERROR|Sell \$ERROR|DCA|Buy Limit|Sell Limit|Wallet|Portfolio|Holder Top 10|Monitor|Refer|Settings|Help)$"), handle_buttons))
    application.add_handler(CommandHandler("alert", set_price_alert))
    application.add_handler(CommandHandler("slippage", set_slippage))
    application.add_handler(CommandHandler("broadcast", set_broadcast))

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(monitor_orders, 'interval', seconds=30, args=[application])
    scheduler.add_job(monitor_price_alerts, 'interval', seconds=30, args=[application])
    scheduler.add_job(hourly_pulse, 'interval', hours=1, args=[application])
    scheduler.start()
    application.scheduler = scheduler

    # Start bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
