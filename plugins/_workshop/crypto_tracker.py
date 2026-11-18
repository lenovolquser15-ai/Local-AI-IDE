"""Crypto and Market Tracker Plugin for LocalHarness.

Provides real-time cryptocurrency prices, 24h volume, and market rankings
via public CoinGecko API (no API key required).
"""
import requests

NAME = "crypto_tracker"


def _crypto_price(cfg, args):
    coin = (args.get("coin") or "bitcoin").lower().strip()
    vs = (args.get("currency") or "usd").lower().strip()
    url = f"https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": coin, "vs_currencies": vs, "include_24hr_change": "true"}
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        if coin not in data:
            return {"ok": False, "error": f"Монета '{coin}' не найдена (используй bitcoin, ethereum, solana, toncoin и т.д.)"}
        val = data[coin].get(vs)
        change = data[coin].get(f"{vs}_24h_change")
        return {
            "ok": True,
            "coin": coin,
            "currency": vs.upper(),
            "price": val,
            "change_24h_percent": round(change, 2) if change is not None else None,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка запроса: {exc}"}


def _crypto_top(cfg, args):
    limit = min(max(int(args.get("limit") or 10), 1), 50)
    vs = (args.get("currency") or "usd").lower().strip()
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": vs, "order": "market_cap_desc", "per_page": limit, "page": 1}
    try:
        r = requests.get(url, params=params, timeout=12)
        items = r.json()
        result = []
        for c in items:
            result.append({
                "rank": c.get("market_cap_rank"),
                "name": c.get("name"),
                "symbol": (c.get("symbol") or "").upper(),
                "price": c.get("current_price"),
                "change_24h": round(c.get("price_change_percentage_24h") or 0.0, 2),
            })
        return {"ok": True, "currency": vs.upper(), "top": result}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка запроса: {exc}"}


TOOLS = [
    {
        "name": "crypto_price",
        "desc": "Узнать актуальную цену и 24ч изменение криптовалюты (bitcoin, ethereum, toncoin, solana)",
        "params": {
            "coin": ("string", "имя криптовалюты (bitcoin, ethereum, solana, toncoin)"),
            "currency": ("string?", "валюта котировки (usd, rub, eur)"),
        },
        "required": ["coin"],
        "fn": _crypto_price,
    },
    {
        "name": "crypto_top",
        "desc": "Получить список топ криптовалют по рыночной капитализации",
        "params": {
            "limit": ("number?", "количество позиций (1..50)"),
            "currency": ("string?", "валюта (usd, rub, eur)"),
        },
        "required": [],
        "fn": _crypto_top,
    },
]
