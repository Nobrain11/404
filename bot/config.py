"""All configuration loaded from environment variables."""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler("error404_bot.log"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default or "")
    if required and not value:
        logging.critical("Missing required env var: %s", name)
        sys.exit(1)
    return value


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str
    group_chat_id: int
    admin_ids: list[int]          # admins who receive notifications

    # Chain
    rpc_url: str
    chain_id: int
    explorer_url: str
    contract_address: str
    weth_address: str
    swap_router: str
    token_symbol: str
    token_decimals: int

    # Infra
    database_url: str
    encryption_key: str
    port: int

    # Branding
    project_website: str
    project_twitter: str
    project_group: str

    # Trading
    default_slippage: float
    whale_threshold: float
    referral_bonus_amount: float

    @property
    def branding(self) -> str:
        return (
            f"🌐 [error404.world]({self.project_website})  "
            f"🐦 [{self.project_twitter}](https://x.com/erro404hood)  "
            f"💬 [{self.project_group}](https://t.me/error404groupofficial)"
        )

    @property
    def branding_plain(self) -> str:
        return f"🌐 {self.project_website} | 🐦 {self.project_twitter} | 💬 {self.project_group}"

    def tx_link(self, tx_hash: str) -> str:
        return f"{self.explorer_url.rstrip('/')}/tx/{tx_hash}"

    def address_link(self, address: str) -> str:
        return f"{self.explorer_url.rstrip('/')}/address/{address}"


def load_settings() -> Settings:
    raw_group = _env("GROUP_CHAT_ID", required=True)
    try:
        group_chat_id = int(raw_group)
    except ValueError:
        logging.critical("GROUP_CHAT_ID must be an integer")
        sys.exit(1)

    raw_admins = _env("ADMIN_IDS", "")
    admin_ids: list[int] = []
    for a in raw_admins.split(","):
        a = a.strip()
        if a.isdigit():
            admin_ids.append(int(a))

    return Settings(
        bot_token=_env("BOT_TOKEN", required=True),
        group_chat_id=group_chat_id,
        admin_ids=admin_ids,
        rpc_url=_env("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
        chain_id=int(_env("CHAIN_ID", "4663")),
        explorer_url=_env("EXPLORER_URL", "https://robinhoodchain.blockscout.com"),
        contract_address=_env("CONTRACT_ADDRESS", "0x4699902aEF95196e4Bceb6472EB131A2c18206fA"),
        weth_address=_env("WETH_ADDRESS", "0x4200000000000000000000000000000000000006"),
        swap_router=_env("SWAP_ROUTER", "0x"),
        token_symbol=_env("TOKEN_SYMBOL", "$ERROR"),
        token_decimals=int(_env("TOKEN_DECIMALS", "18")),
        database_url=_env("DATABASE_URL", "sqlite:///error404.db"),
        encryption_key=_env("ENCRYPTION_KEY", required=True),
        port=int(_env("PORT", "8080")),
        project_website=_env("PROJECT_WEBSITE", "https://www.error404.world/"),
        project_twitter=_env("PROJECT_TWITTER", "@erro404hood"),
        project_group=_env("PROJECT_GROUP", "@error404groupofficial"),
        default_slippage=float(_env("DEFAULT_SLIPPAGE", "1.0")),
        whale_threshold=float(_env("WHALE_THRESHOLD", "1.0")),
        referral_bonus_amount=float(_env("REFERRAL_BONUS_AMOUNT", "100.0")),
    )


DISCLAIMER = (
    "⚠️ _Trading carries risk. $ERROR is a memecoin with high volatility. "
    "Trade responsibly. This bot is for entertainment only._"
)

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
