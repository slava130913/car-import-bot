"""SQLite-хранилище: заказы, заявки, расчёты, переписка с поддержкой и боты клиентов.

Один процесс обслуживает основной бот и боты клиентов (white label). Каждая запись помечена tenant_id:
«main» у основного бота, у клиента это id его бота (username в нижнем регистре). Нагрузка небольшая,
поэтому синхронный sqlite3 с одним соединением.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAIN = "main"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    vin TEXT NOT NULL,
    contact TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    price_stars INTEGER DEFAULT 0,
    telegram_charge_id TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    model TEXT,
    budget TEXT,
    city TEXT,
    contact TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    input_json TEXT NOT NULL,
    total REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_users (
    tenant_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS support (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    admin_msg_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    bot_id INTEGER NOT NULL UNIQUE,
    token TEXT NOT NULL,
    username TEXT NOT NULL,
    brand TEXT NOT NULL,
    manager TEXT NOT NULL DEFAULT '',
    service_fee INTEGER NOT NULL DEFAULT 0,
    owner_id INTEGER NOT NULL,
    admin_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'trial',
    trial_until TEXT,
    paid_until TEXT,
    expiry_notified INTEGER NOT NULL DEFAULT 0,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# Колонки, добавленные после первого релиза: (таблица, колонка, определение)
MIGRATIONS = [
    ("orders", "tenant_id", f"TEXT NOT NULL DEFAULT '{MAIN}'"),
    ("leads", "tenant_id", f"TEXT NOT NULL DEFAULT '{MAIN}'"),
    ("leads", "timeline", "TEXT"),
    ("leads", "calc_summary", "TEXT"),
    ("leads", "status_by", "TEXT"),
    ("leads", "status_at", "TEXT"),
    ("calcs", "tenant_id", f"TEXT NOT NULL DEFAULT '{MAIN}'"),
    ("support", "tenant_id", f"TEXT NOT NULL DEFAULT '{MAIN}'"),
    ("tenants", "reminder_sent", "INTEGER NOT NULL DEFAULT 0"),
]

LEAD_STATUSES = {"new": "🆕 Новая", "work": "🟡 В работе", "won": "✅ Сделка", "lost": "❌ Отказ"}
TENANT_FIELDS = ("brand", "manager", "service_fee", "admin_ids", "owner_id", "token", "status", "trial_until", "paid_until",
                 "expiry_notified", "reminder_sent")
# Статусы бота клиента: trial и active работают; paused поставил владелец платформы; revoked: клиент отозвал токен
RUNNING_STATUSES = ("trial", "active")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # check_same_thread=False: aiogram может вызвать код из пула потоков; запись идёт по одному соединению последовательно
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.commit()

    # users
    def touch_user(self, user_id: int, username: str | None, tenant_id: str = MAIN) -> None:
        now = _now()
        if tenant_id == MAIN:
            self.conn.execute(
                "INSERT INTO users(user_id, username, first_seen, last_seen) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen",
                (user_id, username, now, now),
            )
        else:
            self.conn.execute(
                "INSERT INTO tenant_users(tenant_id, user_id, username, first_seen, last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tenant_id, user_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen",
                (tenant_id, user_id, username, now, now),
            )
        self.conn.commit()

    def all_user_ids(self, tenant_id: str = MAIN) -> list[int]:
        if tenant_id == MAIN:
            return [int(r[0]) for r in self.conn.execute("SELECT user_id FROM users ORDER BY user_id")]
        return [int(r[0]) for r in self.conn.execute(
            "SELECT user_id FROM tenant_users WHERE tenant_id=? ORDER BY user_id", (tenant_id,))]

    # calcs
    def add_calc(self, user_id: int, car: dict[str, Any], total: float | None, tenant_id: str = MAIN) -> int:
        cur = self.conn.execute(
            "INSERT INTO calcs(user_id, input_json, total, created_at, tenant_id) VALUES(?,?,?,?,?)",
            (user_id, json.dumps(car, ensure_ascii=False), total, _now(), tenant_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def last_calc(self, user_id: int, tenant_id: str = MAIN) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT input_json FROM calcs WHERE user_id=? AND tenant_id=? ORDER BY id DESC LIMIT 1", (user_id, tenant_id)
        ).fetchone()
        return json.loads(row["input_json"]) if row else None

    def last_calc_row(self, user_id: int, tenant_id: str = MAIN, days: int = 14) -> dict[str, Any] | None:
        """Последний расчёт пользователя за N дней вместе с итогом: прикладываем его к заявке."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT input_json, total FROM calcs WHERE user_id=? AND tenant_id=? AND created_at>=? ORDER BY id DESC LIMIT 1",
            (user_id, tenant_id, since),
        ).fetchone()
        if not row:
            return None
        return {**json.loads(row["input_json"]), "total": row["total"]}

    # orders
    def add_order(self, user_id: int, username: str | None, vin: str, contact: str, price_stars: int, tenant_id: str = MAIN) -> int:
        status = "awaiting_payment" if price_stars > 0 else "new"
        cur = self.conn.execute(
            "INSERT INTO orders(user_id, username, vin, contact, status, price_stars, created_at, tenant_id) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, username, vin, contact, status, price_stars, _now(), tenant_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_paid(self, order_id: int, charge_id: str, tenant_id: str = MAIN) -> bool:
        """Отмечает оплату только заказа своего бота и только того, что ждёт оплаты."""
        cur = self.conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, telegram_charge_id=? WHERE id=? AND tenant_id=? AND status='awaiting_payment'",
            (_now(), charge_id, order_id, tenant_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_done(self, order_id: int, tenant_id: str | None = None) -> bool:
        q, args = "UPDATE orders SET status='done', done_at=? WHERE id=?", [_now(), order_id]
        if tenant_id is not None:
            q += " AND tenant_id=?"
            args.append(tenant_id)
        cur = self.conn.execute(q, args)
        self.conn.commit()
        return cur.rowcount > 0

    def get_order(self, order_id: int, tenant_id: str | None = None) -> sqlite3.Row | None:
        if tenant_id is None:
            return self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return self.conn.execute("SELECT * FROM orders WHERE id=? AND tenant_id=?", (order_id, tenant_id)).fetchone()

    def list_orders(self, limit: int = 20, open_only: bool = True, tenant_id: str = MAIN) -> list[sqlite3.Row]:
        q = "SELECT * FROM orders WHERE tenant_id=?"
        if open_only:
            q += " AND status != 'done'"
        q += " ORDER BY id DESC LIMIT ?"
        return self.conn.execute(q, (tenant_id, limit)).fetchall()

    # leads
    def add_lead(self, user_id: int, username: str | None, model: str, budget: str, city: str, contact: str,
                 tenant_id: str = MAIN, timeline: str | None = None, calc_summary: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO leads(user_id, username, model, budget, city, contact, created_at, tenant_id, timeline, calc_summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, username, model, budget, city, contact, _now(), tenant_id, timeline, calc_summary),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_lead(self, lead_id: int, tenant_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE id=? AND tenant_id=?", (lead_id, tenant_id)).fetchone()

    def set_lead_status(self, lead_id: int, tenant_id: str, status: str, by: str) -> bool:
        if status not in LEAD_STATUSES:
            raise ValueError(status)
        cur = self.conn.execute(
            "UPDATE leads SET status=?, status_by=?, status_at=? WHERE id=? AND tenant_id=?",
            (status, by, _now(), lead_id, tenant_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_leads(self, limit: int = 20, tenant_id: str = MAIN, open_only: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM leads WHERE tenant_id=?"
        if open_only:
            q += " AND status IN ('new','work')"
        q += " ORDER BY id DESC LIMIT ?"
        return self.conn.execute(q, (tenant_id, limit)).fetchall()

    def lead_report(self, tenant_id: str = MAIN, days: int = 30) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        out = {k: 0 for k in LEAD_STATUSES}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM leads WHERE tenant_id=? AND created_at>=? GROUP BY status", (tenant_id, since)
        ):
            out[r["status"]] = int(r["n"])
        out["total"] = sum(out[k] for k in LEAD_STATUSES)
        out["hot"] = int(self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE tenant_id=? AND created_at>=? AND timeline='now'", (tenant_id, since)
        ).fetchone()[0])
        return out

    # support
    def add_support(self, admin_id: int, admin_msg_id: int, user_id: int, tenant_id: str = MAIN) -> None:
        self.conn.execute(
            "INSERT INTO support(admin_id, admin_msg_id, user_id, created_at, tenant_id) VALUES(?,?,?,?,?)",
            (admin_id, admin_msg_id, user_id, _now(), tenant_id),
        )
        self.conn.commit()

    def support_user(self, admin_id: int, admin_msg_id: int, tenant_id: str = MAIN) -> int | None:
        row = self.conn.execute(
            "SELECT user_id FROM support WHERE admin_id=? AND admin_msg_id=? AND tenant_id=?", (admin_id, admin_msg_id, tenant_id)
        ).fetchone()
        return int(row["user_id"]) if row else None

    # tenants (боты клиентов)
    def add_tenant(self, *, tenant_id: str, bot_id: int, token: str, username: str, brand: str, manager: str,
                   service_fee: int, owner_id: int, trial_days: int = 7) -> None:
        trial_until = (datetime.now(timezone.utc) + timedelta(days=trial_days)).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO tenants(id, bot_id, token, username, brand, manager, service_fee, owner_id, admin_ids, status, trial_until, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, bot_id, token, username, brand, manager, service_fee, owner_id, str(owner_id), "trial", trial_until, _now()),
        )
        self.conn.commit()

    def get_tenant(self, tenant_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()

    def tenant_by_bot(self, bot_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tenants WHERE bot_id=?", (bot_id,)).fetchone()

    def list_tenants(self, running_only: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM tenants"
        if running_only:
            q += " WHERE status IN ('trial','active')"
        return self.conn.execute(q + " ORDER BY created_at").fetchall()

    def update_tenant(self, tenant_id: str, **fields: Any) -> bool:
        bad = set(fields) - set(TENANT_FIELDS)
        if bad:
            raise ValueError(f"unknown tenant fields: {bad}")
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self.conn.execute(f"UPDATE tenants SET {sets} WHERE id=?", (*fields.values(), tenant_id))
        self.conn.commit()
        return cur.rowcount > 0

    def tenants_trial_expired(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tenants WHERE status='trial' AND expiry_notified=0 AND trial_until < ?", (_now(),)
        ).fetchall()

    def tenants_trial_ending(self, days: int = 2) -> list[sqlite3.Row]:
        """Пробный период закончится в ближайшие N дней, напоминание ещё не отправлено."""
        soon = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            "SELECT * FROM tenants WHERE status='trial' AND reminder_sent=0 AND trial_until >= ? AND trial_until < ?", (_now(), soon)
        ).fetchall()

    def tenants_paid_expired(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tenants WHERE status='active' AND expiry_notified=0 AND paid_until IS NOT NULL AND paid_until < ?", (_now(),)
        ).fetchall()

    def tenant_counts(self, tenant_id: str) -> dict[str, int]:
        def one(q: str) -> int:
            return int(self.conn.execute(q, (tenant_id,)).fetchone()[0])

        return {
            "users": one("SELECT COUNT(*) FROM tenant_users WHERE tenant_id=?"),
            "calcs": one("SELECT COUNT(*) FROM calcs WHERE tenant_id=?"),
            "leads": one("SELECT COUNT(*) FROM leads WHERE tenant_id=?"),
        }

    # export
    def export_csv(self, tenant_id: str = MAIN) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["тип", "id", "дата", "user_id", "username", "контакт", "VIN / модель", "статус", "бюджет", "город", "когда покупка", "расчёт"])
        for r in self.conn.execute("SELECT * FROM orders WHERE tenant_id=? ORDER BY id", (tenant_id,)):
            w.writerow(["заказ VIN", r["id"], r["created_at"], r["user_id"], r["username"], r["contact"], r["vin"], r["status"], "", "", "", ""])
        for r in self.conn.execute("SELECT * FROM leads WHERE tenant_id=? ORDER BY id", (tenant_id,)):
            w.writerow(["заявка", r["id"], r["created_at"], r["user_id"], r["username"], r["contact"], r["model"],
                        LEAD_STATUSES.get(r["status"], r["status"]), r["budget"], r["city"], r["timeline"] or "", r["calc_summary"] or ""])
        users_table = "users" if tenant_id == MAIN else "tenant_users"
        join = "u.user_id=c.user_id" + ("" if tenant_id == MAIN else " AND u.tenant_id=c.tenant_id")
        for r in self.conn.execute(
            f"SELECT c.*, u.username FROM calcs c LEFT JOIN {users_table} u ON {join} WHERE c.tenant_id=? ORDER BY c.id", (tenant_id,)
        ):
            w.writerow(["расчёт", r["id"], r["created_at"], r["user_id"], r["username"], "", r["input_json"], "", r["total"], "", "", ""])
        return ("﻿" + buf.getvalue()).encode("utf-8")

    # stats
    def stats(self, tenant_id: str = MAIN) -> dict[str, int]:
        def one(q: str, *args: Any) -> int:
            return int(self.conn.execute(q, args).fetchone()[0])

        users = one("SELECT COUNT(*) FROM users") if tenant_id == MAIN else one(
            "SELECT COUNT(*) FROM tenant_users WHERE tenant_id=?", tenant_id)
        return {
            "users": users,
            "calcs": one("SELECT COUNT(*) FROM calcs WHERE tenant_id=?", tenant_id),
            "calcs_7d": one("SELECT COUNT(*) FROM calcs WHERE tenant_id=? AND created_at >= datetime('now', '-7 days')", tenant_id),
            "orders": one("SELECT COUNT(*) FROM orders WHERE tenant_id=?", tenant_id),
            "orders_paid": one("SELECT COUNT(*) FROM orders WHERE tenant_id=? AND status IN ('paid','done')", tenant_id),
            "leads": one("SELECT COUNT(*) FROM leads WHERE tenant_id=?", tenant_id),
        }
