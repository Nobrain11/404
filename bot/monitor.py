from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from telegram import Bot
from telegram.constants import ParseMode

from .chain import ChainClient, TokenTransfer
from .config import Settings
from .crypto_utils import short_addr
from .models import Transaction, User, get_session

log = logging.getLogger(__name__)

POLL_INTERVAL = 30
MAX_BLOCK_SPAN = 500


class ChainMonitor:
    def __init__(self, bot: Bot, chain: ChainClient, cfg: Settings):
        self.bot = bot
        self.chain = chain
        self.cfg = cfg
        self.last_block: int | None = None
        self._running = False

    def _tracked_wallets(self) -> dict[str, str]:
        with get_session() as session:
            rows = session.execute(select(User.wallet_address, User.username)).all()
        return {a.lower(): (u or "anon") for a, u in rows}

    def _already_seen(self, tx_hash: str) -> bool:
        with get_session() as session:
            return session.scalar(
                select(Transaction).where(Transaction.tx_hash == tx_hash)
            ) is not None

    def _record(self, wallet: str, transfer: TokenTransfer, tx_type: str) -> None:
        with get_session() as session:
            session.add(Transaction(
                wallet_address=wallet,
                tx_hash=transfer.tx_hash,
                tx_type=tx_type,
                amount=transfer.amount,
                token_symbol=self.chain.symbol,
                token_contract=self.cfg.contract_address,
                timestamp=dt.datetime.utcnow(),
                broadcasted=True,
            ))
            session.commit()

    async def _broadcast(self, text: str) -> None:
        try:
            await self.bot.send_message(
                self.cfg.group_chat_id,
                f"{text}\n{self.cfg.branding}",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.error("Broadcast failed: %s", exc)

    async def _handle_transfer(self, transfer: TokenTransfer, tracked: dict[str, str]) -> None:
        sender = transfer.sender.lower()
        recipient = transfer.recipient.lower()
        link = self.cfg.tx_link(transfer.tx_hash)
        amount = f"{transfer.amount:,.4f}".rstrip("0").rstrip(".")

        if transfer.is_mint and recipient in tracked:
            self._record(transfer.recipient, transfer, "mint")
            await self._broadcast(
                f"🎨 @{tracked[recipient]} MINTED Error404 NFT — {amount} $ERROR404 [Tx]({link})"
            )
            return

        if sender in tracked and recipient in tracked:
            self._record(transfer.sender, transfer, "send")
            await self._broadcast(
                f"🔄 @{tracked[sender]} SENT {amount} $ERROR404 → @{tracked[recipient]} [Tx]({link})"
            )
            return

        if sender in tracked:
            self._record(transfer.sender, transfer, "send")
            await self._broadcast(
                f"💸 @{tracked[sender]} SENT {amount} $ERROR404 to `{short_addr(transfer.recipient)}` [Tx]({link})"
            )
            return

        if recipient in tracked:
            self._record(transfer.recipient, transfer, "receive")
            await self._broadcast(
                f"📥 @{tracked[recipient]} RECEIVED {amount} $ERROR404 from `{short_addr(transfer.sender)}` [Tx]({link})"
            )

    async def poll_once(self) -> None:
        head = await self.chain.latest_block()
        if head is None:
            return

        if self.last_block is None:
            self.last_block = max(head - 1, 0)

        if head <= self.last_block:
            return

        from_block = self.last_block + 1
        to_block = min(head, from_block + MAX_BLOCK_SPAN - 1)

        tracked = self._tracked_wallets()
        if tracked:
            transfers = await self.chain.fetch_transfers(from_block, to_block)
            for transfer in transfers:
                if self._already_seen(transfer.tx_hash):
                    continue
                addrs = {transfer.sender.lower(), transfer.recipient.lower()}
                if not addrs & set(tracked):
                    continue
                await self._handle_transfer(transfer, tracked)

        self.last_block = to_block

    async def run(self) -> None:
        self._running = True
        log.info("Chain monitor started (every %ss)", POLL_INTERVAL)
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                log.exception("Monitor cycle error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
