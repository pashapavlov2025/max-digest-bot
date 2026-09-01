"""
Шифрование файлов сессий MAX.

Сессия — это полноценный доступ к чужому мессенджеру, поэтому на диске
она лежит зашифрованной, а расшифровывается только на время работы
с MAX во временный каталог, который потом стирается.
"""

import base64
import hashlib
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet

from .config import config

SESSION_FILE = "session.db"


def _cipher() -> Fernet:
    # Ключ из переменной окружения приводим к нужному формату детерминированно,
    # чтобы можно было задавать любую строку, а не только валидный Fernet-ключ.
    digest = hashlib.sha256(config.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypted_path(telegram_id: int) -> Path:
    return config.sessions_dir / f"{telegram_id}.enc"


def has_session(telegram_id: int) -> bool:
    return _encrypted_path(telegram_id).exists()


def store_session(telegram_id: int, session_path: Path) -> None:
    encrypted = _cipher().encrypt(session_path.read_bytes())
    _encrypted_path(telegram_id).write_bytes(encrypted)


def drop_session(telegram_id: int) -> None:
    _encrypted_path(telegram_id).unlink(missing_ok=True)


@contextmanager
def session_workdir(telegram_id: int) -> Iterator[Path]:
    """
    Готовит временный каталог с расшифрованной сессией.

    После выхода из блока сессия зашифровывается обратно (MAX обновляет токен
    в процессе работы, и потерять это обновление нельзя), а каталог удаляется.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=f"max-{telegram_id}-"))
    session_path = work_dir / SESSION_FILE
    encrypted = _encrypted_path(telegram_id)

    try:
        if encrypted.exists():
            session_path.write_bytes(_cipher().decrypt(encrypted.read_bytes()))
        yield work_dir
        if session_path.exists():
            store_session(telegram_id, session_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
