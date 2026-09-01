"""
Хранилище на SQLite: пользователи, инвайты, состояние онбординга.

Файлы сессий MAX лежат отдельно на диске в зашифрованном виде —
в базе хранится только путь и служебные поля.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    phone         TEXT,
    chat_id       INTEGER,          -- id чата в MAX, который читаем
    chat_title    TEXT,
    digest_time   TEXT DEFAULT '20:00',
    llm_provider  TEXT,             -- NULL = как задано на сервере
    state         TEXT DEFAULT 'new',
    created_at    TEXT DEFAULT (datetime('now')),
    last_digest_at TEXT,
    paused        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER,
    used_by     INTEGER,
    created_at  TEXT DEFAULT (datetime('now')),
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    kind       TEXT,
    detail     TEXT,
    at         TEXT DEFAULT (datetime('now'))
);
"""


@dataclass
class User:
    telegram_id: int
    username: str | None
    phone: str | None
    chat_id: int | None
    chat_title: str | None
    digest_time: str
    llm_provider: str | None
    state: str
    paused: bool

    @property
    def is_ready(self) -> bool:
        return self.state == "ready" and self.chat_id is not None


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        phone=row["phone"],
        chat_id=row["chat_id"],
        chat_title=row["chat_title"],
        digest_time=row["digest_time"] or config.default_digest_time,
        llm_provider=row["llm_provider"],
        state=row["state"],
        paused=bool(row["paused"]),
    )


def get_user(telegram_id: int) -> User | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return _to_user(row) if row else None


def create_user(telegram_id: int, username: str | None) -> User:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, digest_time) VALUES (?, ?, ?)",
            (telegram_id, username, config.default_digest_time),
        )
    user = get_user(telegram_id)
    assert user is not None
    return user


def update_user(telegram_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE users SET {assignments} WHERE telegram_id = ?",
            (*fields.values(), telegram_id),
        )


def delete_user(telegram_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))


def ready_users() -> list[User]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE state = 'ready' AND paused = 0 AND chat_id IS NOT NULL"
        ).fetchall()
        return [_to_user(row) for row in rows]


def all_users() -> list[User]:
    with connect() as conn:
        return [_to_user(row) for row in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()]


# --- Инвайты: бот пускает только по коду ---

def add_invite(code: str, created_by: int) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO invites (code, created_by) VALUES (?, ?)", (code, created_by))


def use_invite(code: str, telegram_id: int) -> bool:
    """Помечает код использованным. False, если кода нет или он уже потрачен."""
    with connect() as conn:
        row = conn.execute("SELECT used_by FROM invites WHERE code = ?", (code,)).fetchone()
        if row is None or row["used_by"] is not None:
            return False
        conn.execute(
            "UPDATE invites SET used_by = ?, used_at = datetime('now') WHERE code = ?",
            (telegram_id, code),
        )
        return True


def free_invites() -> list[str]:
    with connect() as conn:
        return [r["code"] for r in conn.execute("SELECT code FROM invites WHERE used_by IS NULL").fetchall()]


def log_event(telegram_id: int | None, kind: str, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (telegram_id, kind, detail) VALUES (?, ?, ?)",
            (telegram_id, kind, detail[:500]),
        )
