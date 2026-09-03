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
    # Каталоги с данными закрыты от посторонних локальных пользователей:
    # внутри сессии чужих мессенджеров и выжимки из переписки
    @property
    def data_dir(self) -> Path:
        path = Path(os.environ.get("DATA_DIR", "data"))
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def sessions_dir(self) -> Path:
        path = self.data_dir / "sessions"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
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

    # Цена за миллион токенов у выбранной модели. Ноль — значит в отчёте
    # будут только токены: врать про деньги хуже, чем промолчать о них.
    @property
    def price_in(self) -> float:
        return float(os.environ.get("PRICE_IN", "0"))

    @property
    def price_out(self) -> float:
        return float(os.environ.get("PRICE_OUT", "0"))

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

    # --- Чтение фотографий ---
    # Только те, у которых есть подпись или рядом задан вопрос: сплошное чтение
    # по замеру уходит на букеты и снимки с праздника.

    @property
    def vision_enabled(self) -> bool:
        return os.environ.get("VISION_ENABLED", "1") in {"1", "true", "yes"}

    @property
    def vision_model(self) -> str:
        return os.environ.get("VISION_MODEL", "kimi-k2.6")

    # Потолок на одну сводку: каждая картинка — это ещё десяток секунд ожидания
    @property
    def vision_max_photos(self) -> int:
        return int(os.environ.get("VISION_MAX_PHOTOS", "6"))

    # --- Поведение ---
    @property
    def timezone(self) -> str:
        return os.environ.get("TIMEZONE", "Europe/Moscow")

    @property
    def default_digest_time(self) -> str:
        return os.environ.get("DEFAULT_DIGEST_TIME", "20:00")

    # Утреннее напоминание о том, что запланировано на сегодня
    @property
    def default_morning_time(self) -> str:
        return os.environ.get("DEFAULT_MORNING_TIME", "07:30")

    # --- Всплески активности ---
    # Внеочередная сводка, когда в чате внезапно прорвало. Пороги подобраны так,
    # чтобы обычный оживлённый вечер под них не попадал: спам убьёт доверие быстрее,
    # чем пропущенная новость.

    @property
    def burst_enabled(self) -> bool:
        return os.environ.get("BURST_ENABLED", "1") in {"1", "true", "yes"}

    # Меньше этого числа сообщений за час — не всплеск, что бы ни говорила статистика
    @property
    def burst_min_messages(self) -> int:
        return int(os.environ.get("BURST_MIN_MESSAGES", "25"))

    # Во сколько раз час должен быть оживлённее обычного
    @property
    def burst_factor(self) -> float:
        return float(os.environ.get("BURST_FACTOR", "4"))

    # Сколько часов молчим после срабатывания
    @property
    def burst_cooldown_hours(self) -> int:
        return int(os.environ.get("BURST_COOLDOWN_HOURS", "6"))

    # Часы, когда внеочередные сводки не шлём: ночью новость подождёт до утра
    @property
    def quiet_hours(self) -> tuple[int, int]:
        raw = os.environ.get("QUIET_HOURS", "23-8")
        start, _, end = raw.partition("-")
        return int(start), int(end)

    # Сколько сообщений максимум тянем за один сбор
    @property
    def max_messages(self) -> int:
        return int(os.environ.get("MAX_MESSAGES", "3000"))


config = Config()
