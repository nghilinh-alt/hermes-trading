"""
On-chain adapter — fetches basic on-chain metrics.
Uses Glassnode free tier by default; set GLASSNODE_API_KEY for premium.
Falls back to CryptoCompare public API if Glassnode unavailable.
"""
import os
import httpx

SCHEMA_VERSION = "onchain/v1"


class SchemaError(Exception):
    pass


async def fetch(asset: str = "BTC/USDT") -> dict:
    symbol = asset.split("/")[0].upper()
    glassnode_key = os.getenv("GLASSNODE_API_KEY", "")

    data = {}

    if glassnode_key:
        data = await _fetch_glassnode(symbol, glassnode_key)
    else:
        data = await _fetch_cryptocompare(symbol)

    if "schema_version" not in data:
        raise SchemaError("on-chain response missing schema_version")

    return data


async def _fetch_glassnode(symbol: str, api_key: str) -> dict:
    base = "https://api.glassnode.com/v1/metrics"
    headers = {"X-Api-Key": api_key}
    params = {"a": symbol, "i": "24h", "f": "JSON", "timestamp_format": "unix"}

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r_active = await client.get(f"{base}/addresses/active_count", headers=headers, params=params)
            r_active.raise_for_status()
            active_addresses = r_active.json()[-1]["v"] if r_active.json() else None
        except Exception:
            active_addresses = None

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "source": "glassnode",
        "active_addresses_24h": active_addresses,
    }


async def _fetch_cryptocompare(symbol: str) -> dict:
    url = f"https://min-api.cryptocompare.com/data/blockchain/histo/day?fsym={symbol}&limit=1"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
            payload = r.json()
            data_arr = payload.get("Data", {}).get("Data", [])
            latest = data_arr[-1] if data_arr else {}
            return {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "cryptocompare",
                "active_addresses_24h": latest.get("active_addresses"),
                "transaction_count_24h": latest.get("transaction_count"),
                "average_transaction_value": latest.get("average_transaction_value"),
            }
        except Exception:
            return {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "cryptocompare_fallback",
                "active_addresses_24h": None,
                "transaction_count_24h": None,
                "average_transaction_value": None,
            }
