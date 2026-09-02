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


def _log_code_requests() -> None:
    """
    MAX не сообщает, куда именно ушёл код, но в ответе есть его длина,
    остаток попыток и таймауты. Без этого разбирать «код не пришёл» нечем.
    """
    from pymax.api.auth.service import AuthService

    if getattr(AuthService.request_code, "_logged", False):
        return

    original = AuthService.request_code

    async def logged(self, phone: str):  # type: ignore[no-untyped-def]
        response = await original(self, phone)
        log.info(
            "MAX принял запрос кода: длина %s, попыток осталось %s, ждать до %s мс",
            getattr(response, "code_length", "?"),
            getattr(response, "request_count_left", "?"),
            getattr(response, "request_max_duration", "?"),
        )
        return response

    logged._logged = True  # type: ignore[attr-defined]
    AuthService.request_code = logged  # type: ignore[method-assign]


_log_code_requests()

def describe_attachments(message) -> str:
    """
    Короткая пометка о вложениях.

    Читать картинки мы не умеем, но молчать о них нельзя: в родительских
    чатах расписание присылают скриншотом, а решения принимают опросом.
    Пусть модель хотя бы знает, что сообщение было, и скажет «посмотри сам».
    """
    marks = []
    for attach in getattr(message, "attaches", None) or []:
        kind = str(getattr(attach, "type", "")).rsplit(".", 1)[-1].strip("'\"")

        if kind == "POLL":
            title = getattr(attach, "title", "") or "без названия"
            answers = [a.get("text") if isinstance(a, dict) else getattr(a, "text", "")
                       for a in (getattr(attach, "answers", None) or [])]
            state = getattr(attach, "state", None)
            votes = state.get("total") if isinstance(state, dict) else getattr(state, "total", None)
            parts = [f"опрос «{title}»"]
            if answers:
                parts.append("варианты: " + " / ".join(str(a) for a in answers if a))
            if votes:
                parts.append(f"проголосовало {votes}")
            marks.append("; ".join(parts))
        elif kind == "FILE":
            marks.append(f"файл «{getattr(attach, 'name', 'без имени')}»")
        elif kind == "PHOTO":
            marks.append("фото")
        elif kind == "VIDEO":
            marks.append("видео")
        elif kind in {"AUDIO", "VOICE"}:
            marks.append("голосовое сообщение")
        elif kind == "SHARE":
            marks.append("ссылка")

    return f"[{'; '.join(marks)}]" if marks else ""


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


async def list_chats(telegram_id: int, phone: str) -> list[dict]:
    """
    Группы и каналы аккаунта с приметами для выбора.

    Названий мало: в жизни бывает два чата «5 З» — с учителем и без.
    Поэтому отдаём ещё число участников и время последнего сообщения,
    по ним человек опознаёт нужный чат надёжнее, чем по имени.
    """
    with crypto.session_workdir(telegram_id) as work_dir:
        client = Client(phone=phone, work_dir=str(work_dir))
        await client.connect()
        try:
            chats = await client.fetch_chats()
            groups = []
            for chat in chats:
                if not chat.title or str(chat.type or "").upper() not in {"CHAT", "CHANNEL", "GROUP"}:
                    continue
                groups.append(
                    {
                        "id": chat.id,
                        "title": chat.title,
                        "participants": chat.participants_count or 0,
                        "last_event": chat.last_event_time or 0,
                    }
                )
            # Самые живые сверху: нужный чат почти всегда среди них
            return sorted(groups, key=lambda c: c["last_event"], reverse=True)
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
                raw = message.time
                seconds = raw / 1000 if raw and raw > 10**12 else raw
                if not seconds or seconds * 1000 < since_ms:
                    continue

                # Текст и пометка о вложениях — вместе: подпись к фото тоже важна
                text = " ".join(part for part in (message.text or "", describe_attachments(message)) if part).strip()
                if not text:
                    continue

                messages.append(
                    {
                        "time": int(seconds),
                        "author": names.get(message.sender, "Участник"),
                        "text": text,
                    }
                )
            return sorted(messages, key=lambda m: m["time"])
        finally:
            await client.close()
