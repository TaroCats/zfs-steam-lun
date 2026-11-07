"""SQLite 持久层。

设计原则：ZFS / configfs 是事实源，这里只保存无法从内核读到的信息
（CHAP 口令、客户端备注、UI 设置、操作日志）。
启动时会与真实状态对账，孤儿记录自动清理。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    client_iqn  TEXT DEFAULT '',
    target_iqn  TEXT DEFAULT '',
    master      TEXT DEFAULT '',
    chap_user   TEXT DEFAULT '',
    chap_pass   TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS masters_meta (
    name        TEXT PRIMARY KEY,
    note        TEXT DEFAULT '',
    created_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ops (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    action  TEXT,
    target  TEXT,
    detail  TEXT,
    result  TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: Any = None) -> Any:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ---------------------------------------------------------------- clients
def upsert_client(name: str, **fields) -> None:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with conn() as c:
        c.execute(
            f"INSERT INTO clients(name, {cols}) VALUES(?, {placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET {updates}",
            (name, *fields.values()),
        )


def get_client(name: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM clients WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def all_clients() -> dict[str, dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM clients").fetchall()
    return {r["name"]: dict(r) for r in rows}


def delete_client(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM clients WHERE name=?", (name,))


# ---------------------------------------------------------------- master meta
def set_master_meta(name: str, note: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO masters_meta(name, note, created_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET note=excluded.note",
            (name, note, time.strftime("%Y-%m-%d %H:%M:%S")),
        )


def get_master_meta(name: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM masters_meta WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def delete_master_meta(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM masters_meta WHERE name=?", (name,))


# ---------------------------------------------------------------- ops log
def log_op(action: str, target: str = "", detail: str = "", result: str = "ok") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO ops(ts, action, target, detail, result) VALUES(?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), action, target, detail, result),
        )


def list_ops(limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM ops ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
