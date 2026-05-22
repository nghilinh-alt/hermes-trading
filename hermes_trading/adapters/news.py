"""
News adapter — fetches crypto sentiment headlines.
Uses NewsAPI if NEWS_API_KEY is set, else CryptoPanic free public feed.
"""
import os
import httpx

SCHEMA_VERSION = "news/v1"


class SchemaError(Exception):
    pass


async def fetch(asset: str = "BTC/USDT") -> dict:
    symbol = asset.split("/")[0].upper()
    news_key = os.getenv("NEWS_API_KEY", "")

    if news_key:
        result = await _fetch_newsapi(symbol, news_key)
    else:
        result = await _fetch_cryptopanic(symbol)

    if "schema_version" not in result:
        raise SchemaError("news response missing schema_version")

    return result


async def _fetch_newsapi(symbol: str, api_key: str) -> dict:
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": symbol,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": api_key,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            articles = r.json().get("articles", [])
            headlines = [a["title"] for a in articles[:5]]
        except Exception:
            headlines = []

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "source": "newsapi",
        "headlines": headlines,
        "sentiment_hint": _naive_sentiment(headlines),
    }


async def _fetch_cryptopanic(symbol: str) -> dict:
    url = f"https://cryptopanic.com/api/v1/posts/?auth_token=public&currencies={symbol}&public=true"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
            results = r.json().get("results", [])
            headlines = [p.get("title", "") for p in results[:5]]
        except Exception:
            headlines = []

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "source": "cryptopanic",
        "headlines": headlines,
        "sentiment_hint": _naive_sentiment(headlines),
    }


def _naive_sentiment(headlines: list) -> str:
    """Very rough positive/negative/neutral classifier on headline words."""
    positive = {"surge", "rally", "gain", "bull", "rise", "high", "record", "up", "soar", "buy"}
    negative = {"crash", "drop", "fall", "bear", "down", "lose", "low", "sell", "dump", "fear"}
    score = 0
    for h in headlines:
        words = h.lower().split()
        score += sum(1 for w in words if w in positive)
        score -= sum(1 for w in words if w in negative)
    if score > 0:
        return "positive"
    elif score < 0:
        return "negative"
    return "neutral"
