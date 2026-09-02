"""
Хранилище на SQLite: пользователи, их чаты, инвайты, состояние онбординга.

Файлы сессий MAX лежат отдельно на диске в зашифрованном виде —
в базе хранится только путь и служебные поля.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    phone         TEXT,
    chat_id       INTEGER,          -- устарело: чаты живут в user_chats
    chat_title    TEXT,
    digest_time   TEXT DEFAULT '20:00',
    llm_provider  TEXT,             -- NULL = как задано на сервере
    state         TEXT DEFAULT 'new',
    created_at    TEXT DEFAULT (datetime('now')),
    last_digest_at TEXT,
    paused        INTEGER DEFAULT 0
);

-- Чатов у человека обычно несколько: два ребёнка, секция, кружок
CREATE TABLE IF NOT EXISTS user_chats (
    telegram_id INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    chat_title  TEXT,
    added_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (telegram_id, chat_id)
);

-- Что ждёт человека завтра: заполняется вечерней сводкой, читается утром
CREATE TABLE IF NOT EXISTS agenda (
    telegram_id INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    on_date     TEXT NOT NULL,      -- YYYY-MM-DD в часовом поясе сервиса
    chat_title  TEXT,
    items       TEXT NOT NULL,      -- JSON: [{"when": "...", "what": "..."}]
    sent        INTEGER DEFAULT 0,
    PRIMARY KEY (telegram_id, chat_id, on_date)
);

-- Почасовые замеры оживления чата: по ним видно всплеск
CREATE TABLE IF NOT EXISTS activity (
    telegram_id INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    at          TEXT DEFAULT (datetime('now')),
    count       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_lookup ON activity (telegram_id, chat_id, at);

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

# Поля, добавленные после первой версии. Ключ — имя, значение — тип с умолчанием.
LATER_COLUMNS = {
    "morning_time": "TEXT DEFAULT '07:30'",
    "morning": "INTEGER DEFAULT 1",
    "bursts": "INTEGER DEFAULT 1",
    "failures": "INTEGER DEFAULT 0",
    "last_error": "TEXT",
    "last_error_at": "TEXT",
    "last_burst_at": "TEXT",
}


@dataclass
class Chat:
    chat_id: int
    title: str


@dataclass
class User:
    telegram_id: int
    username: str | None
    phone: str | None
    digest_time: str
    morning_time: str
    morning: bool
    bursts: bool
    llm_provider: str | None
    state: str
    paused: bool
    failures: int
    last_error: str | None
    last_burst_at: str | None
    chats: list[Chat] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return self.state == "ready" and bool(self.chats)

    @property
    def titles(self) -> str:
        return ", ".join(chat.title for chat in self.chats) or "—"


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

        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for name, definition in LATER_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

        # Переезд с одного чата на список: старую привязку переносим как есть
        conn.execute(
            """
            INSERT OR IGNORE INTO user_chats (telegram_id, chat_id, chat_title)
            SELECT telegram_id, chat_id, chat_title FROM users WHERE chat_id IS NOT NULL
            """
        )


def _chats_of(conn: sqlite3.Connection, telegram_id: int) -> list[Chat]:
    rows = conn.execute(
        # rowid вторым ключом: чаты добавляются одной пачкой, и по времени они неразличимы
        "SELECT chat_id, chat_title FROM user_chats WHERE telegram_id = ? ORDER BY added_at, rowid",
        (telegram_id,),
    ).fetchall()
    return [Chat(chat_id=row["chat_id"], title=row["chat_title"] or "чат") for row in rows]


def _to_user(conn: sqlite3.Connection, row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        phone=row["phone"],
        digest_time=row["digest_time"] or config.default_digest_time,
        morning_time=row["morning_time"] or config.default_morning_time,
        morning=bool(row["morning"]),
        bursts=bool(row["bursts"]),
        llm_provider=row["llm_provider"],
        state=row["state"],
        paused=bool(row["paused"]),
        failures=row["failures"] or 0,
        last_error=row["last_error"],
        last_burst_at=row["last_burst_at"],
        chats=_chats_of(conn, row["telegram_id"]),
    )


def get_user(telegram_id: int) -> User | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return _to_user(conn, row) if row else None


def create_user(telegram_id: int, username: str | None) -> User:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, digest_time, morning_time) VALUES (?, ?, ?, ?)",
            (telegram_id, username, config.default_digest_time, config.default_morning_time),
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
        for table in ("users", "user_chats", "agenda", "activity"):
            conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (telegram_id,))


def _users_where(clause: str) -> list[User]:
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM users {clause}").fetchall()
        return [_to_user(conn, row) for row in rows]


def ready_users() -> list[User]:
    """Все, кому положены сводки. Без чатов пользователь сюда не попадает."""
    users = _users_where(
        "WHERE state = 'ready' AND paused = 0 AND telegram_id IN (SELECT telegram_id FROM user_chats)"
    )
    return [user for user in users if user.chats]


def all_users() -> list[User]:
    return _users_where("ORDER BY created_at")


# --- Чаты пользователя ---


def set_chats(telegram_id: int, chats: list[tuple[int, str]]) -> None:
    """Заменяет набор чатов целиком: так проще, чем считать разницу."""
    with connect() as conn:
        conn.execute("DELETE FROM user_chats WHERE telegram_id = ?", (telegram_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO user_chats (telegram_id, chat_id, chat_title) VALUES (?, ?, ?)",
            [(telegram_id, chat_id, title) for chat_id, title in chats],
        )
        # Старое поле держим в согласии с новым: на него смотрят бэкапы и глаз админа
        first = chats[0] if chats else (None, None)
        conn.execute(
            "UPDATE users SET chat_id = ?, chat_title = ? WHERE telegram_id = ?",
            (first[0], first[1], telegram_id),
        )


def remove_chat(telegram_id: int, chat_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM user_chats WHERE telegram_id = ? AND chat_id = ?", (telegram_id, chat_id)
        )
        conn.execute("DELETE FROM agenda WHERE telegram_id = ? AND chat_id = ?", (telegram_id, chat_id))


# --- Живучесть: чиним не молча ---


def note_failure(telegram_id: int, error: str) -> int:
    """Считает подряд идущие сбои. Возвращает новое значение счётчика."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE users
               SET failures = COALESCE(failures, 0) + 1,
                   last_error = ?,
                   last_error_at = datetime('now')
             WHERE telegram_id = ?
            """,
            (error[:300], telegram_id),
        )
        row = conn.execute("SELECT failures FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return row["failures"] if row else 0


def note_success(telegram_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE users SET failures = 0, last_error = NULL WHERE telegram_id = ?", (telegram_id,)
        )


# --- Завтрашние дела ---


def save_agenda(telegram_id: int, chat_id: int, chat_title: str, on_date: str, items: list[dict]) -> None:
    """Кладёт список дел на дату. Повторная сводка за тот же день перезаписывает."""
    if not items:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agenda (telegram_id, chat_id, on_date, chat_title, items, sent)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (telegram_id, chat_id, on_date, chat_title, json.dumps(items, ensure_ascii=False)),
        )


def agenda_for(telegram_id: int, on_date: str) -> list[tuple[str, list[dict]]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT chat_title, items FROM agenda WHERE telegram_id = ? AND on_date = ? AND sent = 0",
            (telegram_id, on_date),
        ).fetchall()
        return [(row["chat_title"] or "чат", json.loads(row["items"])) for row in rows]


def mark_agenda_sent(telegram_id: int, on_date: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE agenda SET sent = 1 WHERE telegram_id = ? AND on_date = ?", (telegram_id, on_date)
        )
        # Позавчерашние планы никому не нужны
        conn.execute("DELETE FROM agenda WHERE on_date < date('now', '-3 days')")


# --- Замеры активности ---


def note_activity(telegram_id: int, chat_id: int, count: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO activity (telegram_id, chat_id, count) VALUES (?, ?, ?)",
            (telegram_id, chat_id, count),
        )
        conn.execute("DELETE FROM activity WHERE at < datetime('now', '-14 days')")


def activity_baseline(telegram_id: int, chat_id: int) -> tuple[float, int]:
    """Средняя оживлённость чата за замеры последних двух недель и их число."""
    with connect() as conn:
        row = conn.execute(
            "SELECT AVG(count) AS mean, COUNT(*) AS samples FROM activity WHERE telegram_id = ? AND chat_id = ?",
            (telegram_id, chat_id),
        ).fetchone()
        return (row["mean"] or 0.0), (row["samples"] or 0)


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
