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


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default or "")
    if required and not value:
        logging.critical("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    group_chat_id: int
    rpc_url: str
    chain_id: int
    explorer_url: str
    contract_address: str
    database_url: str
    encryption_key: str
    port: int
    project_website: str
    project_twitter: str
    project_group: str
    token_symbol: str = "$ERROR404"

    @property
    def branding(self) -> str:
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
        logging.critical("GROUP_CHAT_ID must be an integer (e.g. -1001234567890)")
        sys.exit(1)

    return Settings(
        bot_token=_env("BOT_TOKEN", required=True),
        group_chat_id=group_chat_id,
        rpc_url=_env("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
        chain_id=int(_env("CHAIN_ID", "4663")),
        explorer_url=_env("EXPLORER_URL", "https://robinhoodchain.blockscout.com"),
        contract_address=_env("CONTRACT_ADDRESS", "0x4699902aEF95196e4Bceb6472EB131A2c18206fA"),
        database_url=_env("DATABASE_URL", "sqlite:///error404.db"),
        encryption_key=_env("ENCRYPTION_KEY", required=True),
        port=int(_env("PORT", "8080")),
        project_website=_env("PROJECT_WEBSITE", "https://www.error404.world/"),
        project_twitter=_env("PROJECT_TWITTER", "@erro404hood"),
        project_group=_env("PROJECT_GROUP", "@error404groupofficial"),
    )


DISCLAIMER = (
    "⚠️ _Error404 is a memecoin with high volatility. Nothing here is financial "
    "advice — all trading carries risk and this bot is for entertainment only._"
)
