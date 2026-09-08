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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymax import Client
from pymax.versions.catalog import VersionCatalog

from . import crypto, vision

log = logging.getLogger(__name__)

# Версия, которой мы представляемся MAX.
#
# Это не косметика. MAX молча выбрасывает запросы кода от устаревших версий:
# запрос принимается, в ответе честные «шесть цифр, минута», а код не уходит
# никуда — ни SMS, ни в мессенджер. Зашитая в библиотеке 26.25.0 отвалилась
# в начале сентября 2026, и вход перестал работать у всех (PyMax issue #99).
# Проверено вживую: 26.28.0, 26.29.0 и 26.30.1 доставляют код, 26.25.0 — нет.
#
# Отпечатки свежих сборок библиотека держит не у себя, а в удалённом каталоге,
# поэтому версию нельзя просто написать строкой — нужен и каталог.
APP_VERSION = "26.30.1"

_catalog: VersionCatalog | None = None


async def _version() -> tuple[VersionCatalog, str]:
    """
    Каталог отпечатков и версия, с которыми ходим в MAX.

    Каталог тянется по сети один раз на процесс. Если сеть подвела, откатываемся
    на встроенный: вход по коду с ним не работает, но чтение чатов живой сессией
    не сломается — а это то, ради чего бот и запущен.
    """
    global _catalog

    if _catalog is None:
        catalog = VersionCatalog(remote=True)
        try:
            await catalog.load()
            catalog.remote = False  # дальше клиент в сеть за этим не ходит
            catalog.resolve(APP_VERSION)
        except Exception as exc:  # noqa: BLE001 — сеть или каталог, лечится откатом
            log.warning("не удалось получить каталог версий MAX: %s", exc)
            return VersionCatalog(), VersionCatalog.recommended()
        _catalog = catalog

    return _catalog, APP_VERSION


@dataclass
class CodeRequest:
    """
    Что MAX ответил на запрос кода.

    Длина кода нужна, чтобы просить у человека ровно столько цифр, сколько
    ему пришло, а остаток попыток — чтобы отличить «код ещё летит» от
    «запросы кончились». Иначе «кода нет» не разобрать.
    """

    length: int
    attempts_left: int | None
    wait_ms: int

    def __str__(self) -> str:
        return (
            f"длина {self.length or '?'}, "
            f"попыток осталось {'?' if self.attempts_left is None else self.attempts_left}, "
            f"ждать до {self.wait_ms or '?'} мс"
        )


# Ответы MAX на запрос кода, по номеру телефона. PyMax проходит вход внутри
# connect() и наружу этих цифр не отдаёт — перехватываем по дороге.
code_requests: dict[str, CodeRequest] = {}


def _watch_code_requests() -> None:
    """
    MAX не сообщает, куда именно ушёл код, но в ответе есть его длина,
    остаток попыток и таймауты. Без этого разбирать «код не пришёл» нечем.
    """
    from pymax.api.auth.service import AuthService

    if getattr(AuthService.request_code, "_watched", False):
        return

    original = AuthService.request_code

    async def watched(self, phone: str):  # type: ignore[no-untyped-def]
        response = await original(self, phone)
        request = CodeRequest(
            length=getattr(response, "code_length", 0) or 0,
            attempts_left=getattr(response, "request_count_left", None),
            wait_ms=getattr(response, "request_max_duration", 0) or 0,
        )
        code_requests[phone] = request
        log.info("MAX принял запрос кода: %s", request)
        return response

    watched._watched = True  # type: ignore[attr-defined]
    AuthService.request_code = watched  # type: ignore[method-assign]


_watch_code_requests()


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


def photo_links(message) -> list[tuple[int, str]]:
    """Ссылки на фотографии сообщения. Живут недолго — MAX подписывает их сроком."""
    links = []
    for attach in getattr(message, "attaches", None) or []:
        kind = str(getattr(attach, "type", "")).rsplit(".", 1)[-1].strip("'\"")
        url = getattr(attach, "base_url", None)
        if kind == "PHOTO" and url:
            links.append((getattr(attach, "photo_id", 0), url))
    return links


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

    @property
    def code_request(self) -> CodeRequest | None:
        """Ответ MAX на запрос кода — если он вообще был."""
        return code_requests.get(self.phone)

    async def start(self) -> None:
        # Цифры прошлой попытки не должны выдать себя за свежие
        code_requests.pop(self.phone, None)
        catalog, version = await _version()
        self.client = Client(
            phone=self.phone,
            work_dir=str(self.work_dir),
            sms_code_provider=self.sms,
            password_provider=self.password,
            app_version=version,
            catalog=catalog,
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
        code_requests.pop(self.phone, None)
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
        catalog, version = await _version()
        client = Client(
            phone=phone, work_dir=str(work_dir), app_version=version, catalog=catalog
        )
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
        catalog, version = await _version()
        client = Client(
            phone=phone, work_dir=str(work_dir), app_version=version, catalog=catalog
        )
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
                caption = (message.text or "").strip()
                text = " ".join(part for part in (caption, describe_attachments(message)) if part).strip()
                if not text:
                    continue

                messages.append(
                    {
                        "time": int(seconds),
                        "author": names.get(message.sender, "Участник"),
                        "text": text,
                        # Подпись отдельно от текста: по ней решаем, смотреть ли на картинку
                        "caption": caption,
                        "photos": photo_links(message),
                    }
                )

            messages.sort(key=lambda m: m["time"])
            await vision.enrich(telegram_id, messages)
            return messages
        finally:
            await client.close()


async def count_recent(telegram_id: int, phone: str, chat_ids: list[int], minutes: int) -> dict[int, int]:
    """
    Сколько сообщений пришло в каждый чат за последние `minutes` минут.

    Замер для поиска всплесков: модель не нужна, хватает одной страницы истории.
    Если сообщений там больше сотни — вернём сотню, всплеск это всё равно докажет.
    Все чаты обходим в одной сессии MAX: лишние входы — лишний повод для подозрений.
    """
    since_ms = (time.time() - minutes * 60) * 1000
    counts: dict[int, int] = {}

    with crypto.session_workdir(telegram_id) as work_dir:
        catalog, version = await _version()
        client = Client(
            phone=phone, work_dir=str(work_dir), app_version=version, catalog=catalog
        )
        await client.connect()
        try:
            for chat_id in chat_ids:
                page = await client.fetch_history(chat_id=chat_id, backward=PAGE)
                counts[chat_id] = sum(1 for m in page or [] if (m.time or 0) >= since_ms)
                await asyncio.sleep(0.3)
        finally:
            await client.close()

    return counts


# --- Присмотр за доставкой кодов ---
#
# MAX не сообщает, что разлюбил нашу версию: запрос кода он принимает как ни
# в чём не бывало и просто не отправляет код. Сломанный вход при этом не виден
# ниоткуда — сводки у тех, кто уже подключился, продолжают идти. В сентябре
# 2026 это стоило двух дней разбирательств, поэтому теперь проверяем сами.

# По этим словам узнаём сообщение с кодом среди прочих диалогов
CODE_MARKERS = ("профиль MAX", "Код:")


async def newest_versions(count: int = 3) -> list[str]:
    """Самые свежие версии клиента по мнению удалённого каталога."""
    catalog = VersionCatalog(remote=True)
    await catalog.load()
    ranked = sorted(catalog.versions.items(), key=lambda kv: kv[1].build_number, reverse=True)
    return [version for version, _ in ranked[:count]]


async def probe_code(phone: str) -> CodeRequest | None:
    """
    Просит у MAX код и на этом останавливается.

    Вход не завершаем: нам нужен только сам факт, что запрос принят, — и повод
    посмотреть, дошло ли сообщение. Сессия здесь одноразовая и в никуда.
    """
    code_requests.pop(phone, None)
    provider = QueueProvider(timeout=30)

    with tempfile.TemporaryDirectory(prefix="max-probe-") as work_dir:
        catalog, version = await _version()
        client = Client(
            phone=phone,
            work_dir=work_dir,
            sms_code_provider=provider,
            password_provider=provider,
            app_version=version,
            catalog=catalog,
        )
        task = asyncio.create_task(client.connect())
        try:
            for _ in range(60):
                if phone in code_requests:
                    break
                await asyncio.sleep(0.5)
        finally:
            task.cancel()
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — на выходе ошибки уже не важны
                pass

    return code_requests.pop(phone, None)


async def code_arrived(telegram_id: int, phone: str, since_ms: float) -> bool:
    """
    Дошло ли до человека сообщение с кодом после момента `since_ms`.

    Коды MAX кладёт в диалог с ботом «Коды подтверждения», а туда мы попадаем
    той же сессией, что читаем чаты. Групп не касаемся: код приходит в личку.
    """
    with crypto.session_workdir(telegram_id) as work_dir:
        catalog, version = await _version()
        client = Client(
            phone=phone, work_dir=str(work_dir), app_version=version, catalog=catalog
        )
        await client.connect()
        try:
            for chat in await client.fetch_chats():
                if not str(chat.type or "").upper().endswith("DIALOG"):
                    continue
                page = await client.fetch_history(chat_id=chat.id, backward=5)
                for message in page or []:
                    if (message.time or 0) < since_ms:
                        continue
                    text = getattr(message, "text", "") or ""
                    if any(marker in text for marker in CODE_MARKERS):
                        return True
                await asyncio.sleep(0.2)
        finally:
            await client.close()

    return False
