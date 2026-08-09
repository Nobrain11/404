from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .http_utils import run_sync_with_retry

log = logging.getLogger(__name__)

ERC404_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": True, "name": "tokenId", "type": "uint256"},
        ],
        "name": "ERC721Transfer",
        "type": "event",
    },
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass
class TokenTransfer:
    tx_hash: str
    block_number: int
    sender: str
    recipient: str
    amount: float
    token_id: int | None
    is_mint: bool


class ChainClient:
    def __init__(self, rpc_url: str, chain_id: int, contract_address: str):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.contract_address = Web3.to_checksum_address(contract_address)
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.contract = self.w3.eth.contract(address=self.contract_address, abi=ERC404_ABI)
        self.decimals = 18
        self.symbol = "ERROR404"

    async def connect(self) -> bool:
        def _check() -> int:
            if not self.w3.is_connected():
                raise ConnectionError(f"RPC not reachable: {self.rpc_url}")
            return self.w3.eth.block_number

        block = await asyncio.to_thread(run_sync_with_retry, _check, what="RPC connect")
        if block is None:
            return False
        log.info("Connected to Robinhood Chain (id=%s) at block %s", self.chain_id, block)

        def _meta() -> tuple[int, str]:
            return (self.contract.functions.decimals().call(),
                    self.contract.functions.symbol().call())

        meta = await asyncio.to_thread(run_sync_with_retry, _meta, what="token metadata")
        if meta:
            self.decimals, self.symbol = meta
        return True

    async def latest_block(self) -> int | None:
        return await asyncio.to_thread(
            run_sync_with_retry, lambda: self.w3.eth.block_number, what="eth_blockNumber"
        )

    async def fetch_transfers(self, from_block: int, to_block: int) -> list[TokenTransfer]:
        def _logs():
            return self.contract.events.Transfer().get_logs(
                from_block=from_block, to_block=to_block
            )

        raw = await asyncio.to_thread(run_sync_with_retry, _logs, what="get Transfer logs")
        if not raw:
            return []

        divisor = 10 ** self.decimals
        transfers: list[TokenTransfer] = []
        for entry in raw:
            args = entry["args"]
            sender = args["from"]
            recipient = args["to"]
            value = float(args.get("value", 0)) / divisor
            transfers.append(TokenTransfer(
                tx_hash=entry["transactionHash"].hex(),
                block_number=entry["blockNumber"],
                sender=sender,
                recipient=recipient,
                amount=value,
                token_id=None,
                is_mint=sender.lower() == ZERO_ADDRESS,
            ))
        return transfers

    async def native_balance(self, address: str) -> float:
        def _bal() -> float:
            wei = self.w3.eth.get_balance(Web3.to_checksum_address(address))
            return float(Web3.from_wei(wei, "ether"))
        return await asyncio.to_thread(run_sync_with_retry, _bal, what="eth_getBalance") or 0.0

    async def token_balance(self, address: str) -> float:
        def _bal() -> float:
            raw = self.contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
            return float(raw) / (10 ** self.decimals)
        return await asyncio.to_thread(run_sync_with_retry, _bal, what="balanceOf") or 0.0
