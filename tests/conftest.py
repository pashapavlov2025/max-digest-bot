"""
Общая обвязка тестов.

Три вещи задаются здесь раз и навсегда, чтобы не повторяться в каждом файле:
своя база на каждый тест, отрезанная сеть и модель, которая не отвечает,
пока тест сам не скажет, что она должна ответить.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, llm  # noqa: E402
from tests.fakes import FakeBot  # noqa: E402

ENV = {
    "BOT_TOKEN": "тест:токен",
    "SECRET_KEY": "0" * 32,
    "KIMI_API_KEY": "тест",
    "ADMIN_IDS": "900",
    "TIMEZONE": "Europe/Moscow",
    # Зрение выключено по умолчанию: иначе каждая сводка с картинкой
    # полезла бы в сеть. Тест про зрение включает его сам.
    "VISION_ENABLED": "0",
}


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    """Своя база и свои настройки на каждый тест — ничего не протекает между ними."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    db.init()
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """
    Настоящая сеть в тестах запрещена.

    Рвём на уровне транспорта, а не клиента: httpx.MockTransport при этом
    продолжает работать, и тест про чтение картинок может подсунуть свои ответы.
    """

    async def refuse(self, request):
        raise AssertionError(f"тест полез в сеть: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """
    Паузы между страницами и чатами в тестах не выжидаются.

    В жизни они нужны: залп запросов к MAX с одного адреса выглядит
    подозрительно. В тестах это просто секунды простоя, поэтому спим ноль,
    но по-прежнему отдаём управление циклу — порядок задач не меняется.
    """
    real = asyncio.sleep

    async def instant(delay, *args, **kwargs):
        return await real(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", instant)


class FakeLLM:
    """
    Модель, которая отвечает заранее заготовленным.

    Пока ответы не заданы, любое обращение — ошибка теста: молчаливый поход
    в модель означает, что проверяется не то, что написано в названии теста.
    """

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.by_marker: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def returns(self, *replies: str) -> "FakeLLM":
        """Очередь ответов. Последний повторяется, сколько бы раз ни спросили."""
        self.replies.extend(replies)
        return self

    def when(self, marker: str, reply: str) -> "FakeLLM":
        """
        Ответ на обращение с такой приметой в системном промпте.

        На одну сводку модель зовут несколько раз — сама сводка, склейка по
        каждой дате, описание картинок. Без разделения тест получает склейку,
        которой отвечали на сводку, и проверяет не то, что думает.
        """
        self.by_marker.append((marker, reply))
        return self

    async def complete(self, system, prompt, json_mode=False, provider=None) -> str:
        self.calls.append((system, prompt))
        llm.record({"prompt_tokens": 100, "completion_tokens": 20})

        for marker, reply in self.by_marker:
            if marker in system:
                return reply
        if not self.replies:
            raise AssertionError(
                "тест обратился к модели, не задав ответ: fake_llm.returns(...) или .when(...)"
            )
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    @property
    def asked(self) -> int:
        return len(self.calls)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch) -> FakeLLM:
    """Модель-заглушка. Стоит всегда, отвечает только по заказу теста."""
    fake = FakeLLM()
    monkeypatch.setattr(llm, "complete", fake.complete)
    return fake


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def user():
    """Готовый пользователь с двумя чатами — обычный случай в жизни сервиса."""
    person = db.create_user(500, "родитель")
    db.set_chats(500, [(-100, "5 «З» класс"), (-200, "Секция плавания")])
    db.update_user(500, phone="+79990001122", state="ready")
    return db.get_user(500)


@pytest.fixture
def chat(user):
    """Первый чат пользователя — тот, под сводкой которого нажимают кнопки."""
    return user.chats[0]
