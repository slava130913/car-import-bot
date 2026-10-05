"""Заявки с сайта на хостинге reg.ru.

Форма на сайте пишет заявку в файл на хостинге в России (site_api/lead.php). Бот раз в минуту забирает новые
через site_api/leads.php и присылает менеджерам. Токен доступа не хранится отдельно: и бот, и скрипт выкладки
(scripts/deploy_regru.py) считают его из BOT_TOKEN, поэтому настраивать ничего не нужно.
"""

from __future__ import annotations

import hashlib
import hmac

import aiohttp


def site_token(bot_token: str) -> str:
    return hmac.new(bot_token.encode(), b"site-leads", hashlib.sha256).hexdigest()


async def fetch_leads(session: aiohttp.ClientSession, url: str, token: str, after: int) -> tuple[list[dict], int]:
    """Новые заявки с номером больше after и номер последней заявки на сайте (меньше after: файл заявок начат заново)."""
    async with session.get(url, params={"after": str(after)}, headers={"X-Token": token}) as r:
        if r.status != 200:
            raise RuntimeError(f"{url}: HTTP {r.status}")
        data = await r.json(content_type=None)
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"{url}: неожиданный ответ")
    leads = [x for x in data.get("leads") or [] if isinstance(x, dict) and isinstance(x.get("id"), int) and x["id"] > 0]
    last = data.get("last")
    return leads, last if isinstance(last, int) else max([after] + [x["id"] for x in leads])
