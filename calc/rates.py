"""Курсы валют ЦБ РФ с кешем и запасным вариантом из rules.json."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

CBR_JSON_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL_SEC = 12 * 3600

_cache: dict[str, Any] = {"ts": 0.0, "rates": None, "source": None}


async def fetch_cbr() -> dict[str, float]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "car-import-bot/0.1"}) as s:
        async with s.get(CBR_JSON_URL) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
    v = data["Valute"]
    return {
        "CNY": v["CNY"]["Value"] / v["CNY"]["Nominal"],
        "EUR": v["EUR"]["Value"] / v["EUR"]["Nominal"],
        "USD": v["USD"]["Value"] / v["USD"]["Nominal"],
    }


async def get_rates(rules: dict[str, Any]) -> tuple[dict[str, float], str]:
    """Возвращает (курсы, описание источника). Никогда не падает."""
    now = time.time()
    if _cache["rates"] and now - _cache["ts"] < CACHE_TTL_SEC:
        return _cache["rates"], _cache["source"]
    try:
        rates = await fetch_cbr()
        _cache.update(ts=now, rates=rates, source="ЦБ РФ (онлайн)")
        return rates, _cache["source"]
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
        log.warning("CBR rates unavailable: %s", e)
        fb = rules["fallback_rates"]
        rates = {"CNY": fb["CNY"], "EUR": fb["EUR"], "USD": fb["USD"]}
        return rates, f"запасной курс от {fb['date']}"
