"""
Заглушки вместо внешнего мира: Telegram, MAX и модель.

Здесь нарочно нет ни одной проверки — только правдоподобные двойники.
Проверки живут в тестах, а двойники переиспользуются, иначе каждый новый
тест начинает с сочинения собственного фальшивого MAX.
"""

import time
from contextlib import asynccontextmanager


class FakeBot:
    """Запоминает всё, что бот отправил, вместо похода в Telegram."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []
        self.photos: list[tuple[int, str, str | None]] = []
        self.refuse: Exception | None = None

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.refuse:
            raise self.refuse
        self.messages.append((chat_id, text, reply_markup))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if self.refuse:
            raise self.refuse
        self.photos.append((chat_id, photo.filename, caption))

    # --- чтение отправленного ---

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]

    @property
    def last(self) -> str:
        return self.messages[-1][1]

    def said(self, fragment: str) -> bool:
        return any(fragment in text for text in self.texts)


# --- MAX ---


class _Attach:
    def __init__(self, kind: str, **fields) -> None:
        self.type = kind
        for name, value in fields.items():
            setattr(self, name, value)


class _Person:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.names = [type("Name", (), {"name": name})()]


class MaxMessage:
    """Сообщение MAX в том виде, в каком его отдаёт PyMax."""

    def __init__(self, mid, ms, sender, text="", attaches=None) -> None:
        self.id = mid
        self.time = ms
        self.sender = sender
        self.text = text
        self.attaches = attaches or []


def photo(photo_id: int, url: str | None = None) -> _Attach:
    return _Attach("PHOTO", photo_id=photo_id, base_url=url or f"https://max.test/{photo_id}?sig=x")


def poll(title: str, answers: list[str], votes: int = 0) -> _Attach:
    return _Attach("POLL", title=title, answers=[{"text": a} for a in answers], state={"total": votes})


def file(name: str) -> _Attach:
    return _Attach("FILE", name=name)


def voice() -> _Attach:
    return _Attach("VOICE")


def message(mid: int, *, minutes_ago: float = 0, sender: int = 10, text: str = "", attaches=None) -> MaxMessage:
    """Сообщение, отправленное столько-то минут назад."""
    return MaxMessage(mid, (time.time() - minutes_ago * 60) * 1000, sender, text, attaches)


class FakeMax:
    """
    Клиент MAX с заданной историей.

    Страницы отдаёт как настоящий: не больше `backward` штук за раз, вглубь
    по курсору `from_time`. На этом ломается постраничное чтение, поэтому
    двойник честно повторяет именно это поведение.
    """

    def __init__(self, history: list[MaxMessage], chats: list[dict] | None = None) -> None:
        self.history = sorted(history, key=lambda m: m.time, reverse=True)
        self.chats = chats or []
        self.pages = 0
        self.asked: list[int] = []
        self.closed = False

    async def fetch_history(self, chat_id, backward, from_time=None):
        self.pages += 1
        self.asked.append(chat_id)
        pool = [m for m in self.history if from_time is None or m.time < from_time]
        return pool[:backward]

    async def get_users(self, ids):
        return [_Person(uid, f"Человек{uid}") for uid in ids]

    async def fetch_chats(self):
        return self.chats

    async def close(self):
        self.closed = True

    @property
    def chats_asked(self) -> list[int]:
        """Какие чаты спрашивали, по разу каждый: страниц на чат бывает несколько."""
        return list(dict.fromkeys(self.asked))


def install(monkeypatch, client: FakeMax) -> FakeMax:
    """Подменяет вход в MAX на готовый двойник."""
    from app import max_client

    @asynccontextmanager
    async def session(telegram_id, phone):
        yield client

    monkeypatch.setattr(max_client, "_session", session)
    return client


class FakeChat:
    """Чат в том виде, в каком его отдаёт fetch_chats."""

    def __init__(self, chat_id, title, kind="CHAT", participants=20, last_event=0) -> None:
        self.id = chat_id
        self.title = title
        self.type = kind
        self.participants_count = participants
        self.last_event_time = last_event


# --- Telegram: то, на что отвечают обработчики ---


class FakeUser:
    def __init__(self, uid: int, username: str = "родитель") -> None:
        self.id = uid
        self.username = username


class FakeMessage:
    """Сообщение от человека. Ответы складываются в тот же FakeBot."""

    def __init__(self, bot: FakeBot, text: str = "", uid: int = 500) -> None:
        self.bot = bot
        self.text = text
        self.from_user = FakeUser(uid)
        self.chat = type("Chat", (), {"id": uid})()
        self.replies: list[str] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        await self.bot.send_message(self.from_user.id, text, reply_markup)


class FakeCallback:
    """Нажатие на инлайн-кнопку."""

    def __init__(self, bot: FakeBot, data: str, uid: int = 500) -> None:
        self.bot = bot
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage(bot, uid=uid)
        self.answered: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs):
        self.answered.append((text, show_alert))

    @property
    def toast(self) -> str:
        """Что всплыло на кнопке."""
        return self.answered[-1][0] if self.answered else ""

    @property
    def alerted(self) -> bool:
        return bool(self.answered and self.answered[-1][1])
