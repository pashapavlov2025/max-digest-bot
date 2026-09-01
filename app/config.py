"""Настройки сервиса. Всё из окружения, ничего в коде."""

import os
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


class Config:
    # --- Telegram ---
    @property
    def bot_token(self) -> str:
        return _required("BOT_TOKEN")

    @property
    def admin_ids(self) -> set[int]:
        raw = os.environ.get("ADMIN_IDS", "")
        return {int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()}

    # --- Хранилище ---
    @property
    def data_dir(self) -> Path:
        path = Path(os.environ.get("DATA_DIR", "data"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def sessions_dir(self) -> Path:
        path = self.data_dir / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # Ключ шифрования сессий MAX. Без него сервис не стартует:
    # сессия — это доступ к чужому мессенджеру, в открытом виде ей лежать нельзя.
    @property
    def secret_key(self) -> str:
        return _required("SECRET_KEY")

    # --- Модель ---
    @property
    def llm_provider(self) -> str:
        return os.environ.get("LLM_PROVIDER", "kimi").lower()

    @property
    def kimi_api_key(self) -> str:
        return _required("KIMI_API_KEY")

    @property
    def kimi_base_url(self) -> str:
        return os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")

    @property
    def kimi_model(self) -> str:
        return os.environ.get("KIMI_MODEL", "kimi-k2.6")

    # Размышления модели: у kimi-k2.x включены по умолчанию и сильно замедляют ответ
    @property
    def kimi_thinking(self) -> bool:
        return os.environ.get("KIMI_THINKING", "0") in {"1", "true", "yes"}

    @property
    def gigachat_auth_key(self) -> str | None:
        return os.environ.get("GIGACHAT_AUTH_KEY")

    @property
    def gigachat_model(self) -> str:
        return os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Max")

    # --- Поведение ---
    @property
    def timezone(self) -> str:
        return os.environ.get("TIMEZONE", "Europe/Moscow")

    @property
    def default_digest_time(self) -> str:
        return os.environ.get("DEFAULT_DIGEST_TIME", "20:00")

    # Сколько сообщений максимум тянем за один сбор
    @property
    def max_messages(self) -> int:
        return int(os.environ.get("MAX_MESSAGES", "3000"))


config = Config()
