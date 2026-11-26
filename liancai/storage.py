"""SQLite persistence for the procurement service.

The schema keeps an append-only receipt / audit trail next to the mutable
working state.  Business rules never "edit history": a replacement is a new
bid row plus a new receipt, and a withdrawal is an event row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    display TEXT NOT NULL,
    vendor TEXT,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    supplier TEXT NOT NULL,
    zones TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tenders (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bids (
    id TEXT PRIMARY KEY,
    tender TEXT NOT NULL,
    vendor TEXT NOT NULL,
    credential TEXT NOT NULL,
    revision INTEGER NOT NULL,
    receipt_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0,
    whole INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    status_reason TEXT,
    packages TEXT NOT NULL,
    actor TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tender, vendor, revision)
);

CREATE INDEX IF NOT EXISTS bids_tender_vendor ON bids(tender, vendor);
CREATE INDEX IF NOT EXISTS bids_tender_receipt ON bids(tender, receipt_at);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    tender TEXT NOT NULL,
    vendor TEXT,
    bid TEXT,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS receipts_tender ON receipts(tender, at);
CREATE INDEX IF NOT EXISTS receipts_vendor ON receipts(tender, vendor, at);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tender TEXT NOT NULL,
    round INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    dispute TEXT,
    status TEXT NOT NULL,
    plan TEXT NOT NULL,
    baseline INTEGER
);

CREATE TABLE IF NOT EXISTS awards (
    id TEXT PRIMARY KEY,
    tender TEXT NOT NULL,
    key TEXT NOT NULL,
    evaluation INTEGER NOT NULL,
    bid TEXT NOT NULL,
    vendor TEXT NOT NULL,
    kind TEXT NOT NULL,
    package_ids TEXT NOT NULL,
    lots TEXT NOT NULL,
    cents INTEGER NOT NULL,
    labels TEXT NOT NULL,
    groups TEXT NOT NULL,
    status TEXT NOT NULL,
    hold_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    signature_deadline TEXT,
    published_at TEXT,
    signed_at TEXT,
    declined_at TEXT,
    expired_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tender, key)
);

CREATE INDEX IF NOT EXISTS awards_tender ON awards(tender);
CREATE INDEX IF NOT EXISTS awards_vendor ON awards(tender, vendor);

CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    tender TEXT NOT NULL,
    award TEXT NOT NULL UNIQUE,
    vendor TEXT NOT NULL,
    bid TEXT NOT NULL,
    package_ids TEXT NOT NULL,
    lots TEXT NOT NULL,
    cents INTEGER NOT NULL,
    signed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_by TEXT
);

CREATE TABLE IF NOT EXISTS award_attempts (
    id TEXT PRIMARY KEY,
    award TEXT NOT NULL,
    vendor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    response TEXT,
    at TEXT NOT NULL,
    detail TEXT,
    UNIQUE(award, idempotency_key)
);

CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    tender TEXT NOT NULL,
    who TEXT NOT NULL,
    actor TEXT,
    target_type TEXT NOT NULL,
    target_id TEXT,
    orig TEXT,
    affected_scope TEXT NOT NULL,
    claim TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT,
    reevaluation_plan TEXT
);

CREATE INDEX IF NOT EXISTS disputes_tender ON disputes(tender, filed_at);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    detail TEXT
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._write() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        for table, column, ddl in (
            ("awards", "published_at", "TEXT"),
        ):
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._depth += 1
            try:
                yield self._conn
            except Exception:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.rollback()
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write() as db:
            yield db

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write() as db:
            return db.execute(sql, tuple(params))

    def execute_many(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self._write() as db:
            db.executemany(sql, params)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# Small typed accessor helpers used by the service
# --------------------------------------------------------------------------

def audit(
    store: Store,
    *,
    at: str,
    actor: str | None,
    action: str,
    entity: str | None = None,
    detail: Any = None,
) -> None:
    store.execute(
        "INSERT INTO audit (at, actor, action, entity, detail) VALUES (?, ?, ?, ?, ?)",
        (at, actor, action, entity, _dump(detail) if detail is not None else None),
    )


def list_audit(store: Store, *, entity: str | None = None, limit: int = 500) -> list[dict]:
    if entity is None:
        rows = store.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = store.query(
            "SELECT * FROM audit WHERE entity = ? ORDER BY id DESC LIMIT ?", (entity, limit)
        )
    return [
        {
            "id": row["id"],
            "at": row["at"],
            "actor": row["actor"],
            "action": row["action"],
            "entity": row["entity"],
            "detail": _load(row["detail"]),
        }
        for row in rows
    ]
