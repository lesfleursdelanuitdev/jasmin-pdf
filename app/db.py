"""SQLite persistence (a single file in the data directory)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',          -- draft | published
    pdf_file TEXT NOT NULL,                        -- content-addressed, never changes
    revision INTEGER NOT NULL DEFAULT 1,
    definition TEXT NOT NULL,                      -- JSON (pages, layers, elements, stacks)
    product_handles TEXT NOT NULL DEFAULT '[]',    -- Shopify product handles (JSON list)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    filename TEXT,
    path TEXT NOT NULL,
    preview_path TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS designs (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    snapshot TEXT,                                 -- template definition frozen at approval
    "values" TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',          -- draft | approved | ordered
    summary TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,                           -- Shopify order id
    name TEXT,                                     -- e.g. #1001
    email TEXT,
    customer TEXT,
    shop TEXT,
    source TEXT NOT NULL DEFAULT 'shopify',        -- shopify | demo
    status TEXT NOT NULL DEFAULT 'received',       -- received | in_production | shipped
    total TEXT,
    currency TEXT,
    shipping_address TEXT,
    created_at TEXT NOT NULL,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    line_item_id TEXT NOT NULL,
    design_id TEXT,
    title TEXT,
    variant TEXT,
    quantity INTEGER NOT NULL DEFAULT 1,
    print_file TEXT,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | ready | error
    error TEXT,
    UNIQUE(order_id, line_item_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
