import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

import bot.main as bm
from bot.db import DB

ADMIN_ID = 4242


@dataclass
class User:
    id: int
    username: str | None = None


@dataclass
class Doc:
    file_id: str = "file123"


@dataclass
class Msg:
    caption: str
    document: Doc
    from_user: User
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"text": text})


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.docs: list[tuple[int, str, str]] = []
        self.fail = fail

    async def send_document(self, chat_id: int, file_id: str, caption: str = "") -> None:
        if self.fail:
            raise RuntimeError("blocked by user")
        self.docs.append((chat_id, file_id, caption))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(bm, "ADMIN_IDS", {ADMIN_ID})
    return bm.db


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_send_report_closes_order(env):
    oid = env.add_order(77, "client", "LVVDB21B5MD123456", "@client", 0)
    fbot = FakeBot()
    m = Msg(caption=f"/send {oid}", document=Doc(), from_user=User(ADMIN_ID))
    run(bm.admin_send_report(m, fbot))
    assert fbot.docs and fbot.docs[0][0] == 77 and fbot.docs[0][1] == "file123"
    assert "LVVDB21B5MD123456" in fbot.docs[0][2]
    assert env.get_order(oid)["status"] == "done"
    assert "закрыт" in m.outbox[-1]["text"]


def test_send_report_unknown_order_and_failure(env):
    fbot = FakeBot()
    m = Msg(caption="/send 999", document=Doc(), from_user=User(ADMIN_ID))
    run(bm.admin_send_report(m, fbot))
    assert "не найден" in m.outbox[-1]["text"]

    oid = env.add_order(78, None, "LVVDB21B5MD123457", "@x", 0)
    m2 = Msg(caption=f"/send {oid}", document=Doc(), from_user=User(ADMIN_ID))
    run(bm.admin_send_report(m2, FakeBot(fail=True)))
    assert "Не удалось" in m2.outbox[-1]["text"]
    assert env.get_order(oid)["status"] == "new"


def test_send_report_ignored_for_non_admin(env):
    oid = env.add_order(79, None, "LVVDB21B5MD123458", "@x", 0)
    fbot = FakeBot()
    m = Msg(caption=f"/send {oid}", document=Doc(), from_user=User(1))
    run(bm.admin_send_report(m, fbot))
    assert fbot.docs == [] and m.outbox == []
