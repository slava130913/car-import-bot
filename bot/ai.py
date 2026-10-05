"""ИИ в боте: разбор скриншота объявления из Китая и ответы на вопросы по нашим ставкам и гиду.

Включается переменной ANTHROPIC_API_KEY. Без ключа бот работает как раньше, а ИИ-функции
вежливо предлагают ввести данные вручную.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date
from typing import Any

log = logging.getLogger("bot.ai")

AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5")
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "30"))
# Резервная модель на стороне сервера, если основная откажется отвечать по правилам безопасности
FALLBACK_BETA = "server-side-fallback-2026-07-01"

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "is_car_listing": {"type": "boolean"},
        "model_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "price_cny": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "year": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "month": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "engine_cc": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "power_hp": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "fuel": {"anyOf": [{"type": "string", "enum": ["ice", "hybrid_par", "hybrid_seq", "ev"]}, {"type": "null"}]},
        "mileage_km": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "note": {"type": "string"},
    },
    "required": ["is_car_listing", "model_name", "price_cny", "year", "month", "engine_cc", "power_hp", "fuel", "mileage_km", "note"],
    "additionalProperties": False,
}

LISTING_PROMPT = """Это скриншот объявления о продаже автомобиля с китайской площадки (che168, dongchedi, guazi, autohome и подобные) или фото документов машины.
Извлеки характеристики для расчёта стоимости ввоза в Россию.

Правила:
- price_cny: цена в юанях одним числом. «万» означает 10 000: 9.58万 = 95800. Если цен несколько, бери цену продажи, а не новую.
- year и month: дата первой регистрации (首次上牌) или выпуска. Если только год, month = null.
- engine_cc: рабочий объём в см³ (1.5T = 1498 только если объём точно не указан, иначе бери указанный; 2.0T ≈ 1998). Для электромобиля 0.
- power_hp: мощность в л.с. Если указаны кВт (kW), переведи: л.с. = кВт × 1.36, округли. Для гибрида с ДВС бери суммарную, если указана.
- fuel: ice — бензин или дизель; hybrid_par — подключаемый или обычный гибрид, где ДВС крутит колёса (PHEV, DM-i, DHT); hybrid_seq — последовательный гибрид, где ДВС только заряжает батарею (EREV, 增程); ev — электромобиль (纯电).
- mileage_km: пробег в км (万公里 × 10 000).
- Не выдумывай. Если поля нет на изображении, ставь null.
- note: одно короткое предложение по-русски о том, что стоит перепроверить (например, мощность указана пиковая или объём не виден). Пустая строка, если всё ясно.
- is_car_listing = false, если на изображении не объявление о машине и не её документы."""

QA_SYSTEM = """Ты помощник в Telegram-боте, который считает стоимость ввоза машины из Китая в Россию под ключ. Отвечай по-русски, коротко: 2–6 предложений, без markdown-заголовков.

Отвечай только на основе фактов ниже. Если в них нет ответа, так и скажи и предложи нажать «Рассчитать под ключ» или «Написать нам», где ответит человек. Не придумывай ставки, суммы, сроки и законы, которых нет ниже. Не давай юридических гарантий: расчёт ориентировочный.
Если вопрос не про ввоз машин, вежливо скажи, что бот помогает только с машинами из Китая.

Факты:
{facts}"""


class AIError(Exception):
    """ИИ не смог ответить: нет ключа, отказ, сбой сети или непонятный ответ."""


class AI:
    def __init__(self, api_key: str | None = None, model: str = AI_MODEL, client: Any = None, daily_limit: int = AI_DAILY_LIMIT):
        self.model = model
        self.daily_limit = daily_limit
        self._usage: dict[int, list[float]] = defaultdict(list)
        self._client = client
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if self._client is None and key:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=key, timeout=90.0, max_retries=2)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def allow(self, user_id: int) -> bool:
        """Не больше daily_limit обращений к ИИ на человека за сутки: защищает от расходов."""
        now = time.time()
        recent = [t for t in self._usage[user_id] if now - t < 86400]
        self._usage[user_id] = recent
        if len(recent) >= self.daily_limit:
            return False
        recent.append(now)
        return True

    async def _call(self, **kwargs: Any) -> Any:
        if not self.enabled:
            raise AIError("ИИ выключен")
        try:
            resp = await self._client.beta.messages.create(
                model=self.model,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **kwargs,
            )
        except Exception as e:  # noqa: BLE001 — любые сбои API превращаем в понятный отказ
            log.warning("AI request failed: %s", e)
            raise AIError(str(e)) from e
        if getattr(resp, "stop_reason", None) == "refusal":
            raise AIError("модель отказалась отвечать")
        return resp

    @staticmethod
    def _text(resp: Any) -> str:
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()

    async def parse_listing(self, image: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
        resp = await self._call(
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": LISTING_SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image).decode()}},
                    {"type": "text", "text": LISTING_PROMPT},
                ],
            }],
        )
        try:
            data = json.loads(self._text(resp))
        except json.JSONDecodeError as e:
            raise AIError("непонятный ответ модели") from e
        return clean_listing(data)

    async def answer(self, question: str, facts: str) -> str:
        resp = await self._call(
            max_tokens=2000,
            output_config={"effort": "low"},
            system=[{"type": "text", "text": QA_SYSTEM.format(facts=facts), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": question[:2000]}],
        )
        text = self._text(resp)
        if not text:
            raise AIError("пустой ответ")
        return text[:3500]


def clean_listing(d: dict[str, Any]) -> dict[str, Any]:
    """Оставляет только правдоподобные значения: всё сомнительное становится None и будет спрошено вручную."""
    out: dict[str, Any] = {"is_car_listing": bool(d.get("is_car_listing")), "model_name": None, "price_cny": None,
                           "year": None, "month": None, "engine_cc": None, "power_hp": None, "fuel": None,
                           "mileage_km": None, "note": (d.get("note") or "").strip()[:300]}

    def num(key: str, lo: float, hi: float, cast=int):
        v = d.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            return cast(round(v)) if cast is int else float(v)
        return None

    name = d.get("model_name")
    out["model_name"] = name.strip()[:80] if isinstance(name, str) and name.strip() else None
    out["price_cny"] = num("price_cny", 1000, 10_000_000, float)
    out["year"] = num("year", 1990, date.today().year)
    out["month"] = num("month", 1, 12)
    out["engine_cc"] = num("engine_cc", 0, 8000)
    out["power_hp"] = num("power_hp", 1, 2000)
    out["mileage_km"] = num("mileage_km", 0, 2_000_000)
    fuel = d.get("fuel")
    out["fuel"] = fuel if fuel in ("ice", "hybrid_par", "hybrid_seq", "ev") else None
    if out["fuel"] == "ev" and out["engine_cc"] is None:
        out["engine_cc"] = 0
    return out


def build_facts(rules: dict[str, Any], guide: dict[str, str], explain: dict[str, str], explain_titles: dict[str, str]) -> str:
    """Короткая выжимка ставок и гида, на которую опирается ИИ в ответах."""
    pref = rules["util"]["preferential"]
    costs = rules["costs"]
    lines = [
        f"Правила расчёта актуальны на {rules['version']}.",
        f"Льготный утильсбор для физлица: {pref['new_rub']} ₽ за машину до 3 лет, {pref['old_rub']} ₽ старше 3 лет. Условия: " + "; ".join(pref["conditions"]) + ".",
        "Если льгота не действует, утильсбор считается по коммерческой сетке по объёму и мощности: от сотен тысяч до нескольких миллионов рублей.",
        "Пошлина для физлиц: машины до 3 лет 48–54% от стоимости с минимумом в евро за см³; 3–5 лет и старше 5 лет только в евро за см³ (Решение Совета ЕЭК № 107).",
        "Электромобили и последовательные гибриды: пошлина 15%, акциз по мощности, НДС 22%.",
        "Логистика: " + "; ".join(f"{v['label']}: {v['low']:,}–{v['high']:,} ₽, {v['days']} дней".replace(",", " ") for v in costs["logistics"].values()) + ".",
        f"СБКТС и ЭРА-ГЛОНАСС: {costs['sbkts_glonass']['low']:,}–{costs['sbkts_glonass']['high']:,} ₽. ЭПТС и регистрация: {costs['epts_registration']['low']:,}–{costs['epts_registration']['high']:,} ₽.".replace(",", " "),
        "Бот умеет: расчёт под ключ, выбор из 24 популярных моделей, сравнение маршрутов, проверку льготы, гид, заказ отчёта по VIN, заявку на подбор, чтение скриншота объявления.",
    ]
    for k, v in explain.items():
        lines.append(f"{explain_titles.get(k, k)}: {v}")
    for v in guide.values():
        lines.append(v)
    return "\n".join(lines)
