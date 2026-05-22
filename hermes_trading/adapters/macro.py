"""
Macro adapter — fetches macro context (DXY, fear/greed index).
Uses free public endpoints only.
"""
import httpx

SCHEMA_VERSION = "macro/v1"


class SchemaError(Exception):
    pass


async def fetch(asset: str = "BTC/USDT") -> dict:
    fear_greed = await _fetch_fear_greed()
    result = {
        "schema_version": SCHEMA_VERSION,
        "fear_greed_index": fear_greed.get("value"),
        "fear_greed_label": fear_greed.get("value_classification"),
        "fear_greed_timestamp": fear_greed.get("timestamp"),
    }
    if "schema_version" not in result:
        raise SchemaError("macro response missing schema_version")
    return result


async def _fetch_fear_greed() -> dict:
    url = "https://api.alternative.me/fng/?limit=1"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json().get("data", [{}])
            return data[0] if data else {}
        except Exception:
            return {"value": None, "value_classification": "unknown", "timestamp": None}
