"""SQLite-хранилище заказов, заявок и расчётов. Нагрузка MVP маленькая, поэтому синхронный sqlite3."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # users
    def touch_user(self, user_id: int, username: str | None) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO users(user_id, username, first_seen, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen",
            (user_id, username, now, now),
        )
        self.conn.commit()

    # calcs
    def add_calc(self, user_id: int, car: dict[str, Any], total: float | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO calcs(user_id, input_json, total, created_at) VALUES(?,?,?,?)",
            (user_id, json.dumps(car, ensure_ascii=False), total, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # orders
    def add_order(self, user_id: int, username: str | None, vin: str, contact: str, price_stars: int) -> int:
        status = "awaiting_payment" if price_stars > 0 else "new"
        cur = self.conn.execute(
            "INSERT INTO orders(user_id, username, vin, contact, status, price_stars, created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, username, vin, contact, status, price_stars, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_paid(self, order_id: int, charge_id: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, telegram_charge_id=? WHERE id=?",
            (_now(), charge_id, order_id),
        )
        self.conn.commit()

    def mark_done(self, order_id: int) -> bool:
        cur = self.conn.execute("UPDATE orders SET status='done', done_at=? WHERE id=?", (_now(), order_id))
        self.conn.commit()
        return cur.rowcount > 0

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def list_orders(self, limit: int = 20, open_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM orders"
        if open_only:
            q += " WHERE status != 'done'"
        q += " ORDER BY id DESC LIMIT ?"
        return self.conn.execute(q, (limit,)).fetchall()

    # leads
    def add_lead(self, user_id: int, username: str | None, model: str, budget: str, city: str, contact: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO leads(user_id, username, model, budget, city, contact, created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, username, model, budget, city, contact, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_leads(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # stats
    def stats(self) -> dict[str, int]:
        def one(q: str) -> int:
            return int(self.conn.execute(q).fetchone()[0])

        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "calcs": one("SELECT COUNT(*) FROM calcs"),
            "calcs_7d": one("SELECT COUNT(*) FROM calcs WHERE created_at >= datetime('now', '-7 days')"),
            "orders": one("SELECT COUNT(*) FROM orders"),
            "orders_paid": one("SELECT COUNT(*) FROM orders WHERE status IN ('paid','done')"),
            "leads": one("SELECT COUNT(*) FROM leads"),
        }
