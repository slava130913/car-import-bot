"""Регрессии по итогам ревью ботов клиентов: оплата, гонки опроса, смена токена, напоминания, сотрудники, режим платформы."""

from __future__ import annotations

import asyncio

from aiogram.filters import CommandObject

import bot.main as bm
from bot import tenants as tn
from tests.test_tenants import (  # noqa: F401  (фикстура env используется pytest по имени)
    CLIENT, CLIENT_ADMIN, PLATFORM_ADMIN, Cb, FakeBot, Msg, User, _add_client, env, run, texts,
)


def test_payment_bound_to_tenant_order_and_amount(env):
    db = bm.db
    own = db.add_order(1, "u", "LVVDB21B5MD123456", "+7", 500, "main")
    foreign = db.add_order(2, "v", "LVVDB21B5MD654321", "+7", 0, "client_bot")
    with tn.use(None):
        assert bm._payable_order(f"vin:{own}", "XTR", 500, 1) is not None
        assert bm._payable_order(f"vin:{own}", "XTR", 1, 1) is None        # сумма не та
        assert bm._payable_order(f"vin:{own}", "XTR", 500, 2) is None      # чужой покупатель
        assert bm._payable_order(f"vin:{foreign}", "XTR", 0, 2) is None    # заказ другого бота
    with tn.use(CLIENT):
        assert bm._payable_order(f"vin:{own}", "XTR", 500, 1) is None      # из бота клиента чужой заказ не виден
    assert db.mark_paid(own, "ch1", "client_bot") is False and db.get_order(own)["status"] == "awaiting_payment"
    assert db.mark_paid(own, "ch1", "main") is True and db.mark_paid(own, "ch2", "main") is False


def test_runner_start_is_idempotent_under_race():
    class PollBot:
        def __init__(self):
            self.id = 77
            self.session = self

        async def delete_webhook(self, **kw):
            await asyncio.sleep(0.01)

        async def get_updates(self, **kw):
            await asyncio.sleep(10)

        async def close(self):
            pass

    class Dp:
        def resolve_used_update_types(self):
            return []

    async def flow():
        r = tn.Runner(Dp())
        res = await asyncio.gather(r.start(PollBot()), r.start(PollBot()))
        polls = [t for t in asyncio.all_tasks() if t.get_name() == "poll-77"]
        await r.stop(77)
        await asyncio.sleep(0)
        left = [t for t in asyncio.all_tasks() if t.get_name() == "poll-77" and not t.done()]
        return res, polls, left

    res, polls, left = run(flow())
    assert sorted(res) == [False, True] and len(polls) == 1 and left == []


def test_runner_reports_conflict(monkeypatch):
    from aiogram.exceptions import TelegramConflictError
    from aiogram.methods import GetUpdates

    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(tn.asyncio, "sleep", fast_sleep)

    class ConflictBot:
        def __init__(self):
            self.id = 88
            self.session = self
            self.n = 0

        async def delete_webhook(self, **kw):
            return True

        async def get_updates(self, **kw):
            self.n += 1
            if self.n > 6:
                raise asyncio.CancelledError
            raise TelegramConflictError(method=GetUpdates(), message="Conflict: terminated by setWebhook, webhook is active")

        async def close(self):
            pass

    class Dp:
        def resolve_used_update_types(self):
            return []

    async def flow():
        r = tn.Runner(Dp())
        seen = []

        async def on_conflict(bot, err):
            seen.append(bot.id)

        r.on_conflict = on_conflict
        await r.start(ConflictBot())
        try:
            await r.tasks[88]
        except asyncio.CancelledError:
            pass
        return r, seen

    r, seen = run(flow())
    assert seen == [88] and r.health[88] == "conflict"


def test_middleware_drops_unknown_bot_in_production(env, monkeypatch):
    monkeypatch.setattr(tn, "MAIN_BOT_ID", 1)
    called = []

    async def handler(event, data):
        called.append(tn.current().id)

    run(tn.tenant_middleware(handler, object(), {"bot": FakeBot(999)}))
    run(tn.tenant_middleware(handler, object(), {"bot": FakeBot(1)}))
    assert called == ["main"]


def test_token_rotation_after_revoke(env, monkeypatch):
    state = env
    db = bm.db
    # номер бота совпадает с началом токена, как у настоящих ботов; смена токена у @BotFather номер не меняет
    db.add_tenant(tenant_id="client_bot", bot_id=7770000, token="7770000:" + "C" * 35, username="client_bot",
                  brand="АвтоМост", manager="@m", service_fee=0, owner_id=CLIENT_ADMIN)
    db.update_tenant("client_bot", status="revoked")
    new_token = "7770000:" + "N" * 35

    async def fake_validate(token):
        return (7770000, "client_bot")

    monkeypatch.setattr(bm, "validate_token", fake_validate)

    async def send(user_id, token):
        m = Msg(text="/mybot", from_user=User(id=user_id))
        await bm.wl_start(m, state)
        await bm.wl_token(Msg(text=token, from_user=User(id=user_id), outbox=m.outbox), state)
        return m

    assert "другим владельцем" in texts(run(send(999, new_token)))
    assert db.get_tenant("client_bot")["token"] != new_token
    assert "снова работает" in texts(run(send(CLIENT_ADMIN, new_token)))
    row = db.get_tenant("client_bot")
    assert row["token"] == new_token and row["status"] == "trial" and 7770000 in tn.BY_BOT
    # бота, приостановленного платформой, новый токен не возобновляет
    db.update_tenant("client_bot", status="paused")
    assert "приостановлен" in texts(run(send(CLIENT_ADMIN, "7770000:" + "Z" * 35)))
    assert db.get_tenant("client_bot")["status"] == "paused"


def test_onboarding_same_username_new_bot(env, monkeypatch):
    state = env
    _add_client(bm.db)  # client_bot с bot_id 777

    async def fake_validate(token):
        return (4040, "client_bot")

    monkeypatch.setattr(bm, "validate_token", fake_validate)
    owner = User(id=31337)

    async def flow():
        m = Msg(text="/mybot", from_user=owner)
        await bm.wl_start(m, state)
        await bm.wl_token(Msg(text="4040404:" + "Q" * 35, from_user=owner, outbox=m.outbox), state)
        await bm.wl_brand(Msg(text="Новый", from_user=owner, outbox=m.outbox), state)
        await bm.wl_manager(Msg(text="нет", from_user=owner, outbox=m.outbox), state)
        await bm.wl_fee(Msg(text="0", from_user=owner, outbox=m.outbox), state)
        await bm.wl_confirm(Cb(data="wl:go", message=Msg(outbox=m.outbox), from_user=owner), state, FakeBot())
        return m

    m = run(flow())
    assert "Готово!" in texts(m) and bm.db.get_tenant("client_bot_4040")["bot_id"] == 4040


def test_billing_reminders(env):
    db = bm.db
    db.add_tenant(tenant_id="ending_bot", bot_id=11, token="11:" + "E" * 35, username="ending_bot", brand="E",
                  manager="", service_fee=0, owner_id=1, trial_days=1)
    db.add_tenant(tenant_id="paid_bot", bot_id=12, token="12:" + "P" * 35, username="paid_bot", brand="P",
                  manager="", service_fee=0, owner_id=1, trial_days=7)
    db.update_tenant("paid_bot", status="active", paid_until="2020-01-01T00:00:00+00:00")
    main_bot = FakeBot()
    run(bm.check_billing(main_bot))
    assert db.get_tenant("ending_bot")["reminder_sent"] == 1
    assert db.get_tenant("paid_bot")["expiry_notified"] == 1
    assert any("Закончилась оплата у @paid_bot" in s[1] for s in main_bot.sent)
    main_bot.sent.clear()
    run(bm.check_billing(main_bot))
    assert main_bot.sent == []  # повторно не пишем


def test_tenant_on_extends_from_paid_until(env):
    db = bm.db
    _add_client(db)
    db.update_tenant("client_bot", status="active", paid_until="2099-01-01T00:00:00+00:00")

    async def flow():
        m = Msg(text="/tenant_on client_bot 30", from_user=User(id=PLATFORM_ADMIN))
        await bm.platform_tenant_switch(m, CommandObject(prefix="/", command="tenant_on", args="client_bot 30"), FakeBot())
        return m

    run(flow())
    assert db.get_tenant("client_bot")["paid_until"].startswith("2099-01-31")


def test_admin_management_owner_only(env, monkeypatch):
    db = bm.db
    _add_client(db)
    db.update_tenant("client_bot", admin_ids=f"{CLIENT_ADMIN},8888")

    async def no_configure(bot, t, commands):
        return None

    monkeypatch.setattr(tn, "configure_bot", no_configure)

    async def cmd(name, arg, user):
        t = bm._tenant_from_row(db.get_tenant("client_bot"))
        with tn.use(t):
            m = Msg(text=f"/{name} {arg}".strip(), from_user=User(id=user))
            await bm.tenant_settings(m, CommandObject(prefix="/", command=name, args=arg or None), FakeBot(777))
            return m

    assert "только владелец" in texts(run(cmd("addadmin", "9999", 8888)))
    assert "(владелец)" in texts(run(cmd("admins", "", 8888)))
    assert "Владельца убрать нельзя" in texts(run(cmd("deladmin", str(CLIENT_ADMIN), CLIENT_ADMIN)))
    run(cmd("deladmin", "8888", CLIENT_ADMIN))
    assert db.get_tenant("client_bot")["admin_ids"] == str(CLIENT_ADMIN)
    run(cmd("setmanager", "нет", CLIENT_ADMIN))
    assert db.get_tenant("client_bot")["manager"] == ""


def test_vin_intro_in_client_bot(env):
    async def flow():
        with tn.use(CLIENT):
            m = Msg(text="/vin")
            await bm._vin_intro(m)
            return m

    out = texts(run(flow()))
    assert "менеджер АвтоМост" in out and "24 часов" not in out


def test_platform_mode_off_hides_platform(env, monkeypatch):
    state = env
    monkeypatch.setattr(bm, "PLATFORM_MODE", False)

    async def flow():
        m = Msg(text="/mybot", from_user=User(id=PLATFORM_ADMIN))
        await bm.wl_start(m, state)
        p = Msg(text="/start partner")
        await bm.cmd_start(p, state, CommandObject(prefix="/", command="start", args="partner"))
        t = Msg(text="/tenants", from_user=User(id=PLATFORM_ADMIN))
        await bm.platform_tenants(t)
        return m, p, t

    m, p, t = run(flow())
    assert m.outbox == [] and "Запустить свой бот" not in texts(p) and t.outbox == []


def test_tenants_list_is_chunked(env):
    db = bm.db
    for i in range(40):
        db.add_tenant(tenant_id=f"bot{i}_bot", bot_id=1000 + i, token=f"{1000 + i}:" + "X" * 35, username=f"bot{i}_bot",
                      brand="Очень длинное название компании " + "А" * 25, manager="", service_fee=0, owner_id=1)

    async def flow():
        m = Msg(text="/tenants", from_user=User(id=PLATFORM_ADMIN))
        await bm.platform_tenants(m)
        return m

    m = run(flow())
    assert len(m.outbox) >= 2 and all(len(o["text"]) <= 4096 for o in m.outbox)
    assert "bot39_bot" in texts(m)


def test_support_handler_skips_commands():
    handler = next(h for h in bm.router.message.handlers if h.callback is bm.support_message)
    assert len(handler.filters) >= 2  # состояние и «не команда»


def test_stray_token_deleted(env):
    state = env

    async def flow():
        with tn.use(CLIENT):
            m = Msg(text="123456:" + "S" * 35)
            await bm.stray_token(m, state)
            return m

    m = run(flow())
    assert m.deleted and "удалил" in texts(m)
