"""30-second on-chain monitor."""
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

    def _tracked_wallets(self) -> dict[str, tuple[str, bool, bool]]:
        with get_session() as session:
            rows = session.execute(
                select(User.wallet_address, User.username, User.monitor_enabled, User.broadcast_enabled)
            ).all()
        return {a.lower(): (u or "anon", m, b) for a, u, m, b in rows}

    def _user_tid(self, address: str) -> int | None:
        with get_session() as session:
            user = session.scalar(select(User).where(User.wallet_address.ilike(address)))
            return user.telegram_id if user else None

    def _already_seen(self, tx_hash: str) -> bool:
        with get_session() as session:
            return session.scalar(select(Transaction).where(Transaction.tx_hash == tx_hash)) is not None

    def _record(self, wallet: str, transfer: TokenTransfer, tx_type: str) -> None:
        with get_session() as session:
            session.add(Transaction(
                wallet_address=wallet, tx_hash=transfer.tx_hash, tx_type=tx_type,
                amount=transfer.amount, token_symbol=self.chain.symbol,
                token_contract=self.cfg.contract_address,
                timestamp=dt.datetime.utcnow(), broadcasted=True,
            ))
            session.commit()

    async def _group(self, text: str) -> None:
        try:
            await self.bot.send_message(self.cfg.group_chat_id, f"{text}\n{self.cfg.branding}",
                                         parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        except Exception as exc:
            log.error("Group msg failed: %s", exc)

    async def _dm(self, tid: int, text: str) -> None:
        try:
            await self.bot.send_message(tid, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        except Exception as exc:
            log.warning("DM to %s failed: %s", tid, exc)

    async def _handle(self, transfer: TokenTransfer, tracked: dict) -> None:
        sender = transfer.sender.lower()
        recipient = transfer.recipient.lower()
        link = self.cfg.tx_link(transfer.tx_hash)
        amt = f"{transfer.amount:,.4f}".rstrip("0").rstrip(".")
        sym = self.cfg.token_symbol
        whale = " 🐋 *WHALE*" if transfer.amount >= self.cfg.whale_threshold else ""

        if transfer.is_mint and recipient in tracked:
            u, _, b = tracked[recipient]
            self._record(transfer.recipient, transfer, "mint")
            if b:
                await self._group(f"🎨 @{u} MINTED Error404 NFT — {amt} {sym} [Tx]({link})")
            tid = self._user_tid(transfer.recipient)
            if tracked[recipient][1] and tid:
                await self._dm(tid, f"🎨 You minted {amt} {sym}! [Tx]({link})")
            return

        if sender in tracked and recipient in tracked:
            su, sm, sb = tracked[sender]
            ru, rm, rb = tracked[recipient]
            self._record(transfer.sender, transfer, "send")
            if sb:
                await self._group(f"🔄 @{su} SENT {amt} {sym} → @{ru} [Tx]({link})")
            sid, rid = self._user_tid(transfer.sender), self._user_tid(transfer.recipient)
            if sm and sid: await self._dm(sid, f"💸 You sent {amt} {sym} to @{ru} [Tx]({link})")
            if rm and rid: await self._dm(rid, f"📥 You received {amt} {sym} from @{su} [Tx]({link})")
            return

        if sender in tracked:
            u, monitor, broadcast = tracked[sender]
            self._record(transfer.sender, transfer, "send")
            if broadcast:
                await self._group(f"💸 @{u} SENT {amt} {sym} to `{short_addr(transfer.recipient)}` [Tx]({link}){whale}")
            tid = self._user_tid(transfer.sender)
            if monitor and tid:
                await self._dm(tid, f"💸 You sent {amt} {sym} to `{short_addr(transfer.recipient)}` [Tx]({link})")
            return

        if recipient in tracked:
            u, monitor, broadcast = tracked[recipient]
            self._record(transfer.recipient, transfer, "receive")
            if broadcast:
                await self._group(f"📥 @{u} RECEIVED {amt} {sym} from `{short_addr(transfer.sender)}` [Tx]({link}){whale}")
            tid = self._user_tid(transfer.recipient)
            if monitor and tid:
                await self._dm(tid, f"📥 You received {amt} {sym} from `{short_addr(transfer.sender)}` [Tx]({link})")

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
            for t in transfers:
                if self._already_seen(t.tx_hash):
                    continue
                if not {t.sender.lower(), t.recipient.lower()} & set(tracked):
                    continue
                await self._handle(t, tracked)
        self.last_block = to_block

    async def run(self) -> None:
        self._running = True
        log.info("Monitor started (every %ss)", POLL_INTERVAL)
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                log.exception("Monitor error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
