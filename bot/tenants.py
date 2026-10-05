"""Боты клиентов (white label) в одном процессе с основным ботом.

Каждый входящий апдейт обрабатывается от имени «арендатора»: основного бота или бота клиента. Текущий
арендатор хранится в ContextVar, поэтому обработчики берут бренд, админов и цены через current(), а не из
глобальных настроек. Опрос Telegram идёт отдельной задачей на каждого бота, новых клиентов можно
подключать и отключать без перезапуска.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

log = logging.getLogger("tenants")


@dataclass(frozen=True)
class Tenant:
    id: str
    brand: str = ""
    manager: str = ""
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    web_url: str = ""
    vin_price: int = 0
    payment_text: str = ""
    service_fee: int = 0
    is_main: bool = True
    username: str = ""
    status: str = "active"

    def extras(self) -> list[tuple[str, float]]:
        """Строки компании в расчёте: её комиссия или услуги."""
        if self.service_fee > 0:
            return [(f"Услуги {self.brand}" if self.brand else "Услуги компании", float(self.service_fee))]
        return []


_CURRENT: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar("tenant", default=None)
# Функция, которая собирает основного арендатора из текущих настроек модуля бота (так тесты могут их подменять)
_main_factory: Callable[[], Tenant] = lambda: Tenant(id="main")  # noqa: E731
# bot_id → арендатор-клиент; основного бота здесь нет
BY_BOT: dict[int, Tenant] = {}
# id основного бота. Пока не задан (тесты), неизвестный бот считается основным; в работе апдейты
# от ботов, которых нет ни в BY_BOT, ни здесь (например, клиент только что поставлен на паузу), отбрасываются
MAIN_BOT_ID: int | None = None


def set_main_factory(factory: Callable[[], Tenant]) -> None:
    global _main_factory
    _main_factory = factory


def current() -> Tenant:
    return _CURRENT.get() or _main_factory()


def use(tenant: Tenant | None):
    """Контекст для тестов и фоновых задач: выполнить код от имени арендатора."""
    class _Ctx:
        def __enter__(self):
            self.token = _CURRENT.set(tenant)
            return tenant

        def __exit__(self, *exc):
            _CURRENT.reset(self.token)
            return False

    return _Ctx()


def from_row(row: Any, web_url: str, payment_text: str) -> Tenant:
    admins = frozenset(int(x) for x in str(row["admin_ids"]).replace(" ", "").split(",") if x)
    return Tenant(
        id=row["id"], brand=row["brand"], manager=row["manager"] or "", admin_ids=admins, web_url=web_url,
        vin_price=0, payment_text=payment_text, service_fee=int(row["service_fee"] or 0), is_main=False,
        username=row["username"], status=row["status"],
    )


def tenant_web_url(base: str, t: Tenant) -> str:
    """Ссылка на веб-калькулятор в оформлении клиента: его бот, название и комиссия."""
    if not base:
        return ""
    q = urllib.parse.urlencode({"bot": t.username, "brand": t.brand, "fee": t.service_fee})
    return base + ("&" if "?" in base else "?") + q


async def tenant_middleware(handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict) -> Any:
    bot: Bot | None = data.get("bot")
    t = BY_BOT.get(bot.id) if bot is not None else None
    if t is None and bot is not None and MAIN_BOT_ID is not None and bot.id != MAIN_BOT_ID:
        log.warning("update for unknown or paused bot %s dropped", bot.id)
        return None
    token = _CURRENT.set(t)
    try:
        return await handler(event, data)
    finally:
        _CURRENT.reset(token)


async def configure_bot(bot: Bot, t: Tenant, commands: list[BotCommand]) -> None:
    """Описание, команды и кнопка меню бота клиента под его бренд. Ошибки не критичны."""
    calls = [
        bot.set_my_commands(commands),
        bot.set_my_short_description(
            short_description=f"{t.brand}: расчёт машины из Китая под ключ и заявка на подбор."[:120]),
        bot.set_my_description(description=(
            f"Бот компании {t.brand}. Считаю, сколько стоит пригнать машину из Китая под ключ: пошлина, утильсбор, "
            "логистика, СБКТС, ЭПТС и доставка до города. Принимаю заявки на подбор, менеджер свяжется с вами.")[:512]),
    ]
    if t.web_url:
        calls.append(bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Калькулятор", web_app=WebAppInfo(url=t.web_url))))
    for call in calls:
        try:
            await call
        except Exception as e:  # noqa: BLE001
            log.warning("configure %s failed: %s", t.id, e)


class Runner:
    """Опрашивает Telegram за несколько ботов и передаёт апдейты в общий диспетчер."""

    def __init__(self, dp: Dispatcher):
        self.dp = dp
        self.tasks: dict[int, asyncio.Task] = {}
        self.bots: dict[int, Bot] = {}
        self.on_unauthorized: Callable[[Bot], Awaitable[None]] | None = None
        # Вызывается один раз, когда опрос бота стабильно конфликтует (чужой вебхук или второй процесс)
        self.on_conflict: Callable[[Bot, str], Awaitable[None]] | None = None
        self.health: dict[int, str] = {}
        self._pending: set[asyncio.Task] = set()

    def running(self, bot_id: int) -> bool:
        task = self.tasks.get(bot_id)
        return task is not None and not task.done()

    async def start(self, bot: Bot) -> bool:
        """Запускает опрос бота. Задача регистрируется до первого await, поэтому два одновременных вызова
        не создадут два опроса одного бота. Возвращает False, если бот уже опрашивается."""
        if self.running(bot.id):
            return False
        self.bots[bot.id] = bot
        self.health[bot.id] = "ok"
        self.tasks[bot.id] = asyncio.create_task(self._run(bot), name=f"poll-{bot.id}")
        return True

    async def _run(self, bot: Bot) -> None:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("delete_webhook %s: %s", bot.id, e)
        await self._poll(bot)

    async def stop(self, bot_id: int) -> None:
        task = self.tasks.pop(bot_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        bot = self.bots.pop(bot_id, None)
        if bot:
            await bot.session.close()

    async def _feed(self, bot: Bot, update: Any) -> None:
        try:
            await self.dp.feed_update(bot, update)
        except Exception:  # noqa: BLE001
            log.exception("update failed for bot %s", bot.id)

    async def _poll(self, bot: Bot) -> None:
        offset: int | None = None
        allowed = self.dp.resolve_used_update_types()
        backoff = 1.0
        conflicts = 0
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=allowed)
                backoff = 1.0
                if conflicts:
                    conflicts = 0
                    self.health[bot.id] = "ok"
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                log.error("bot %s: token revoked, stopping", bot.id)
                self.health[bot.id] = "revoked"
                if self.on_unauthorized:
                    await self.on_unauthorized(bot)
                return
            except TelegramConflictError as e:
                # Чужой вебхук или второй процесс с тем же токеном: снимаем вебхук и сообщаем, если не проходит
                conflicts += 1
                self.health[bot.id] = "conflict"
                log.warning("poll %s conflict #%d: %s", bot.id, conflicts, e)
                if "webhook" in str(e).lower():
                    try:
                        await bot.delete_webhook(drop_pending_updates=False)
                    except Exception:  # noqa: BLE001
                        pass
                if conflicts == 5 and self.on_conflict:
                    try:
                        await self.on_conflict(bot, str(e))
                    except Exception:  # noqa: BLE001
                        log.exception("on_conflict failed")
                await asyncio.sleep(min(5 * conflicts, 60))
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("poll %s: %s", bot.id, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates:
                offset = upd.update_id + 1
                task = asyncio.create_task(self._feed(bot, upd))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

    async def wait(self) -> None:
        while True:
            await asyncio.sleep(3600)
