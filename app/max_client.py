"""
Работа с MAX через PyMax.

Здесь две задачи:
  1. Интерактивный вход — диалог на несколько минут, где код и пароль
     приходят не из консоли, а сообщениями в Telegram.
  2. Чтение истории чата — постранично, потому что MAX отдаёт максимум
     100 сообщений за запрос и отвергает большие значения backward.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymax import Client

from . import crypto

log = logging.getLogger(__name__)

PAGE = 100
MAX_PAGES = 40
# Сколько ждём код и пароль от человека, прежде чем считать вход неудавшимся
INPUT_TIMEOUT = 600


class QueueProvider:
    """Отдаёт PyMax значение, которое пользователь пришлёт в Telegram."""

    def __init__(self, timeout: int = INPUT_TIMEOUT) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.timeout = timeout
        self.asked = asyncio.Event()

    async def _get(self) -> str:
        self.asked.set()
        return await asyncio.wait_for(self.queue.get(), timeout=self.timeout)

    async def get_code(self, phone: str) -> str:
        return await self._get()

    async def get_password(self, *args, **kwargs) -> str:
        return await self._get()


@dataclass
class LoginFlow:
    """
    Одна попытка входа. Живёт в памяти, пока человек вводит код и пароль.

    PyMax проходит авторизацию внутри connect(), запрашивая значения у
    провайдеров, — поэтому вход крутится фоновой задачей, а бот лишь подкладывает
    ответы пользователя в очереди.
    """

    telegram_id: int
    phone: str
    work_dir: Path
    sms: QueueProvider = field(default_factory=QueueProvider)
    password: QueueProvider = field(default_factory=QueueProvider)
    task: asyncio.Task | None = None
    client: Client | None = None
    error: str | None = None

    async def start(self) -> None:
        self.client = Client(
            phone=self.phone,
            work_dir=str(self.work_dir),
            sms_code_provider=self.sms,
            password_provider=self.password,
        )
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self.client is not None
        try:
            await self.client.connect()
        except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
            self.error = str(exc)
            log.warning("вход не удался для %s: %s", self.telegram_id, exc)
            raise

    async def wait_for_code_prompt(self, timeout: float = 30) -> bool:
        """Дожидается момента, когда MAX действительно запросил код."""
        try:
            await asyncio.wait_for(self.sms.asked.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def needs_password(self) -> bool:
        return self.password.asked.is_set()

    async def submit_code(self, code: str) -> None:
        await self.sms.queue.put(code)

    async def submit_password(self, value: str) -> None:
        await self.password.queue.put(value)

    async def finish(self, timeout: float = 60) -> None:
        """Ждёт завершения входа. Бросает исключение, если MAX отказал."""
        assert self.task is not None
        await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001 — на выходе ошибки уже не важны
                pass
        if self.task is not None and not self.task.done():
            self.task.cancel()


async def list_chats(telegram_id: int, phone: str) -> list[tuple[int, str, str]]:
    """Возвращает (id, тип, название) — только группы и каналы."""
    with crypto.session_workdir(telegram_id) as work_dir:
        client = Client(phone=phone, work_dir=str(work_dir))
        await client.connect()
        try:
            chats = await client.fetch_chats()
            return [
                (chat.id, str(chat.type or ""), chat.title or "(без названия)")
                for chat in chats
                if chat.title
            ]
        finally:
            await client.close()


async def fetch_window(telegram_id: int, phone: str, chat_id: int, hours: int, limit: int) -> list[dict]:
    """
    Сообщения чата за последние `hours` часов.

    MAX отдаёт максимум 100 штук за запрос, поэтому идём вглубь страницами:
    курсором служит время самого старого сообщения предыдущей страницы.
    """
    since_ms = (time.time() - hours * 3600) * 1000

    with crypto.session_workdir(telegram_id) as work_dir:
        client = Client(phone=phone, work_dir=str(work_dir))
        await client.connect()
        try:
            collected: dict[int, object] = {}
            cursor: float | None = None

            for _ in range(MAX_PAGES):
                options = {"chat_id": chat_id, "backward": PAGE}
                if cursor is not None:
                    options["from_time"] = cursor
                page = await client.fetch_history(**options)
                if not page:
                    break

                fresh = [m for m in page if m.id not in collected]
                collected.update({m.id: m for m in page})
                times = [m.time for m in page if m.time]
                if not fresh or not times:
                    break

                oldest = min(times)
                if oldest <= since_ms or oldest == cursor or len(collected) >= limit:
                    break
                cursor = oldest
                await asyncio.sleep(0.3)

            history = list(collected.values())
            senders = {m.sender for m in history if m.sender}
            users = await client.get_users(list(senders)) if senders else []
            names = {u.id: (u.names[0].name if u.names else str(u.id)) for u in users}

            messages = []
            for message in history:
                if not message.text:
                    continue
                raw = message.time
                seconds = raw / 1000 if raw and raw > 10**12 else raw
                if not seconds or seconds * 1000 < since_ms:
                    continue
                messages.append(
                    {
                        "time": int(seconds),
                        "author": names.get(message.sender, "Участник"),
                        "text": message.text,
                    }
                )
            return sorted(messages, key=lambda m: m["time"])
        finally:
            await client.close()
