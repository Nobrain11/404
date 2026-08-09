"""Live price and market stats via DexScreener + Blockscout."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .http_utils import http_get_json

log = logging.getLogger(__name__)


@dataclass
class MarketStats:
    price_usd: float = 0.0
    price_eth: float = 0.0
    change_24h: float = 0.0
    market_cap: float = 0.0
    liquidity: float = 0.0
    volume_24h: float = 0.0
    buys_24h: int = 0
    sells_24h: int = 0
    holders: int = 0
    total_supply: float = 0.0


async def fetch_market_stats(contract_address: str, explorer_url: str, chain_client=None) -> MarketStats:
    stats = MarketStats()

    dex_data = await http_get_json(
        f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}"
    )
    if dex_data:
        pairs = dex_data.get("pairs") or []
        if pairs:
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
            try:
                stats.price_usd  = float(pair.get("priceUsd", 0) or 0)
                stats.change_24h = float((pair.get("priceChange") or {}).get("h24", 0) or 0)
                stats.liquidity  = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                stats.volume_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)
                txns = (pair.get("txns") or {}).get("h24", {})
                stats.buys_24h   = int(txns.get("buys", 0) or 0)
                stats.sells_24h  = int(txns.get("sells", 0) or 0)
            except Exception as exc:
                log.warning("DexScreener parse error: %s", exc)

    token_data = await http_get_json(
        f"{explorer_url.rstrip('/')}/api/v2/tokens/{contract_address}"
    )
    if token_data:
        try:
            stats.holders      = int(token_data.get("holders_count", 0) or 0)
            stats.total_supply = float(token_data.get("total_supply", 0) or 0) / 1e18
            if stats.total_supply and stats.price_usd:
                stats.market_cap = stats.total_supply * stats.price_usd
        except Exception as exc:
            log.warning("Blockscout parse error: %s", exc)

    if chain_client:
        stats.price_eth = await chain_client.token_price_eth()

    return stats


async def fetch_top_holders(contract_address: str, explorer_url: str, limit: int = 10) -> list[dict]:
    data = await http_get_json(
        f"{explorer_url.rstrip('/')}/api/v2/tokens/{contract_address}/holders",
        params={"limit": limit},
    )
    if not data:
        return []
    return [
        {"address": item.get("address", {}).get("hash", ""), "balance": float(item.get("value", 0) or 0) / 1e18}
        for item in (data.get("items") or [])[:limit]
    ]
