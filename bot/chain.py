"""Robinhood Chain client — reads state and executes trades via Uniswap V3."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .http_utils import run_sync_retry

log = logging.getLogger(__name__)

ERC20_ABI = [
    {"constant": True,  "inputs": [],                                         "name": "decimals",    "outputs": [{"name": "", "type": "uint8"}],   "type": "function"},
    {"constant": True,  "inputs": [],                                         "name": "symbol",      "outputs": [{"name": "", "type": "string"}],  "type": "function"},
    {"constant": True,  "inputs": [],                                         "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True,  "inputs": [{"name": "owner",   "type": "address"}],  "name": "balanceOf",   "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"},
                                    {"name": "amount",  "type": "uint256"}],  "name": "approve",     "outputs": [{"name": "", "type": "bool"}],    "type": "function"},
    {"constant": True,  "inputs": [{"name": "owner",   "type": "address"},
                                    {"name": "spender", "type": "address"}],  "name": "allowance",   "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "from",  "type": "address"},
            {"indexed": True,  "name": "to",    "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer", "type": "event",
    },
]

ROUTER_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "deadline",          "type": "uint256"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "amountOutMinimum",  "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ], "name": "params", "type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
]

POOL_ABI = [
    {
        "inputs": [], "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view", "type": "function",
    },
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
POOL_FEE = 3000


@dataclass
class TokenTransfer:
    tx_hash: str
    block_number: int
    sender: str
    recipient: str
    amount: float
    is_mint: bool


@dataclass
class TradeResult:
    success: bool
    tx_hash: str | None
    error: str | None
    amount_out: float = 0.0


class ChainClient:
    def __init__(self, rpc_url: str, chain_id: int, contract_address: str,
                 weth_address: str, swap_router: str, decimals: int = 18):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.contract_address = Web3.to_checksum_address(contract_address)
        self.weth_address = Web3.to_checksum_address(weth_address)
        self.swap_router = swap_router
        self.decimals = decimals
        self.symbol = "ERROR"
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.token = self.w3.eth.contract(address=self.contract_address, abi=ERC20_ABI)
        self.router = (
            self.w3.eth.contract(address=Web3.to_checksum_address(swap_router), abi=ROUTER_ABI)
            if swap_router and swap_router != "0x" else None
        )
        self._pool = None

    async def connect(self) -> bool:
        def _check():
            if not self.w3.is_connected():
                raise ConnectionError(f"RPC unreachable: {self.rpc_url}")
            return self.w3.eth.block_number
        block = await asyncio.to_thread(run_sync_retry, _check, what="RPC connect")
        if block is None:
            return False
        log.info("Connected to Robinhood Chain id=%s at block %s", self.chain_id, block)
        def _meta():
            return self.token.functions.decimals().call(), self.token.functions.symbol().call()
        meta = await asyncio.to_thread(run_sync_retry, _meta, what="token meta")
        if meta:
            self.decimals, self.symbol = meta
        return True

    def set_pool(self, pool_address: str) -> None:
        self._pool = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=POOL_ABI
        )

    async def latest_block(self) -> int | None:
        return await asyncio.to_thread(
            run_sync_retry, lambda: self.w3.eth.block_number, what="blockNumber"
        )

    async def native_balance(self, address: str) -> float:
        def _b():
            return float(Web3.from_wei(
                self.w3.eth.get_balance(Web3.to_checksum_address(address)), "ether"
            ))
        return await asyncio.to_thread(run_sync_retry, _b, what="ethBalance") or 0.0

    async def token_balance(self, address: str) -> float:
        def _b():
            raw = self.token.functions.balanceOf(Web3.to_checksum_address(address)).call()
            return float(raw) / (10 ** self.decimals)
        return await asyncio.to_thread(run_sync_retry, _b, what="tokenBalance") or 0.0

    async def total_supply(self) -> float:
        def _s():
            return float(self.token.functions.totalSupply().call()) / (10 ** self.decimals)
        return await asyncio.to_thread(run_sync_retry, _s, what="totalSupply") or 0.0

    async def token_price_eth(self) -> float:
        if self._pool is None:
            return 0.0
        def _p():
            slot = self._pool.functions.slot0().call()
            sqrt_price_x96 = slot[0]
            if sqrt_price_x96 == 0:
                return 0.0
            price = (sqrt_price_x96 / (2 ** 96)) ** 2
            if self.contract_address.lower() < self.weth_address.lower():
                return price
            return 1.0 / price if price > 0 else 0.0
        return await asyncio.to_thread(run_sync_retry, _p, what="slot0 price") or 0.0

    async def fetch_transfers(self, from_block: int, to_block: int) -> list[TokenTransfer]:
        def _logs():
            return self.token.events.Transfer().get_logs(from_block=from_block, to_block=to_block)
        raw = await asyncio.to_thread(run_sync_retry, _logs, what="Transfer logs")
        if not raw:
            return []
        div = 10 ** self.decimals
        return [
            TokenTransfer(
                tx_hash=e["transactionHash"].hex(),
                block_number=e["blockNumber"],
                sender=e["args"]["from"],
                recipient=e["args"]["to"],
                amount=float(e["args"].get("value", 0)) / div,
                is_mint=e["args"]["from"].lower() == ZERO_ADDRESS,
            )
            for e in raw
        ]

    async def get_gas_price(self, strategy: str = "medium") -> int:
        def _gp():
            base = self.w3.eth.gas_price
            return int(base * {"slow": 0.8, "medium": 1.0, "fast": 1.3}.get(strategy, 1.0))
        return await asyncio.to_thread(run_sync_retry, _gp, what="gasPrice") or 0

    async def get_nonce(self, address: str) -> int:
        def _n():
            return self.w3.eth.get_transaction_count(Web3.to_checksum_address(address), "pending")
        return await asyncio.to_thread(run_sync_retry, _n, what="nonce") or 0

    async def buy_error(self, wallet_address: str, private_key: str,
                        eth_amount: float, slippage_pct: float = 1.0,
                        gas_strategy: str = "medium") -> TradeResult:
        if self.router is None:
            return TradeResult(False, None, "Swap router not configured")
        try:
            addr = Web3.to_checksum_address(wallet_address)
            amount_in_wei = Web3.to_wei(eth_amount, "ether")
            price = await self.token_price_eth()
            if price <= 0:
                return TradeResult(False, None, "Unable to fetch price")
            expected_out = eth_amount / price
            amount_out_min = int(expected_out * (1 - slippage_pct / 100) * (10 ** self.decimals))
            gas_price = await self.get_gas_price(gas_strategy)
            nonce = await self.get_nonce(addr)
            deadline = int(time.time()) + 300

            def _build_and_send():
                tx = self.router.functions.exactInputSingle({
                    "tokenIn": self.weth_address, "tokenOut": self.contract_address,
                    "fee": POOL_FEE, "recipient": addr, "deadline": deadline,
                    "amountIn": amount_in_wei, "amountOutMinimum": amount_out_min,
                    "sqrtPriceLimitX96": 0,
                }).build_transaction({
                    "from": addr, "value": amount_in_wei,
                    "gasPrice": gas_price, "nonce": nonce, "chainId": self.chain_id,
                })
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
                signed = self.w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            receipt = await asyncio.to_thread(run_sync_retry, _build_and_send, what="buy swap")
            if receipt and receipt.status == 1:
                return TradeResult(True, receipt.transactionHash.hex(), None, expected_out)
            return TradeResult(False, None, "Transaction reverted")
        except Exception as exc:
            log.error("Buy failed: %s", exc)
            return TradeResult(False, None, str(exc))

    async def sell_error(self, wallet_address: str, private_key: str,
                         token_amount: float, slippage_pct: float = 1.0,
                         gas_strategy: str = "medium") -> TradeResult:
        if self.router is None:
            return TradeResult(False, None, "Swap router not configured")
        try:
            addr = Web3.to_checksum_address(wallet_address)
            amount_in = int(token_amount * (10 ** self.decimals))
            price = await self.token_price_eth()
            if price <= 0:
                return TradeResult(False, None, "Unable to fetch price")
            expected_eth = token_amount * price
            amount_out_min = int(Web3.to_wei(expected_eth * (1 - slippage_pct / 100), "ether"))
            gas_price = await self.get_gas_price(gas_strategy)
            nonce = await self.get_nonce(addr)
            deadline = int(time.time()) + 300

            def _approve_and_send():
                allowance = self.token.functions.allowance(addr, self.router.address).call()
                current_nonce = nonce
                if allowance < amount_in:
                    approve_tx = self.token.functions.approve(
                        self.router.address, amount_in * 10
                    ).build_transaction({
                        "from": addr, "gasPrice": gas_price, "nonce": nonce, "chainId": self.chain_id,
                    })
                    approve_tx["gas"] = int(self.w3.eth.estimate_gas(approve_tx) * 1.2)
                    signed_a = self.w3.eth.account.sign_transaction(approve_tx, private_key)
                    approve_hash = self.w3.eth.send_raw_transaction(signed_a.raw_transaction)
                    self.w3.eth.wait_for_transaction_receipt(approve_hash, timeout=60)
                    current_nonce = nonce + 1

                swap_tx = self.router.functions.exactInputSingle({
                    "tokenIn": self.contract_address, "tokenOut": self.weth_address,
                    "fee": POOL_FEE, "recipient": addr, "deadline": deadline,
                    "amountIn": amount_in, "amountOutMinimum": amount_out_min,
                    "sqrtPriceLimitX96": 0,
                }).build_transaction({
                    "from": addr, "gasPrice": gas_price, "nonce": current_nonce, "chainId": self.chain_id,
                })
                swap_tx["gas"] = int(self.w3.eth.estimate_gas(swap_tx) * 1.2)
                signed = self.w3.eth.account.sign_transaction(swap_tx, private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            receipt = await asyncio.to_thread(run_sync_retry, _approve_and_send, what="sell swap")
            if receipt and receipt.status == 1:
                return TradeResult(True, receipt.transactionHash.hex(), None, expected_eth)
            return TradeResult(False, None, "Transaction reverted")
        except Exception as exc:
            log.error("Sell failed: %s", exc)
            return TradeResult(False, None, str(exc))

    async def send_token(self, wallet_address: str, private_key: str,
                         to_address: str, token_amount: float,
                         gas_strategy: str = "medium") -> TradeResult:
        try:
            addr = Web3.to_checksum_address(wallet_address)
            to_addr = Web3.to_checksum_address(to_address)
            amount = int(token_amount * (10 ** self.decimals))
            gas_price = await self.get_gas_price(gas_strategy)
            nonce = await self.get_nonce(addr)

            def _send():
                tx = self.token.functions.transfer(to_addr, amount).build_transaction({
                    "from": addr, "gasPrice": gas_price, "nonce": nonce, "chainId": self.chain_id,
                })
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
                signed = self.w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            receipt = await asyncio.to_thread(run_sync_retry, _send, what="token transfer")
            if receipt and receipt.status == 1:
                return TradeResult(True, receipt.transactionHash.hex(), None, token_amount)
            return TradeResult(False, None, "Transaction reverted")
        except Exception as exc:
            log.error("Transfer failed: %s", exc)
            return TradeResult(False, None, str(exc))
