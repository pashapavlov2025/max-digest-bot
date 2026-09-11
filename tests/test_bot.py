"""
Слой бота: кнопки, состояние диалога и адресация чата.

Самое хрупкое здесь — «текущий чат»: он живёт в состоянии диалога между
нажатием кнопки под сводкой и следующим сообщением человека. Если он
протечёт или потеряется, вопрос молча уйдёт не туда.
"""

import time

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, service
from app.bot import commands, keyboards, texts
from tests.fakes import FakeCallback, FakeMessage


@pytest.fixture
def state() -> FSMContext:
    """Состояние диалога — настоящее, на памяти."""
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=500, user_id=500))


@pytest.fixture
def asked(monkeypatch) -> list:
    """Перехватывает вопросы, уходящие в сборку ответа."""
    calls = []

    async def answer_question(bot, user, question, days=30, chat=None):
        calls.append({"question": question, "chat": chat})

    monkeypatch.setattr(service, "answer_question", answer_question)
    return calls


@pytest.fixture
def shown(monkeypatch) -> list:
    """Перехватывает просьбы показать фотографии."""
    calls = []

    async def send_photos(bot, user, chat, hours, until=None):
        calls.append({"chat": chat, "hours": hours, "until": until})
        return 0

    monkeypatch.setattr(service, "send_photos", send_photos)
    return calls


# --- Кнопки под сводкой ---


def test_кнопки_несут_чат_и_окно():
    keyboard = keyboards.under_digest(-100, 72, until=1_757_600_000)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["ask:-100", "pics:-100:72:1757600000"]


def test_кнопка_помечается_текущим_моментом():
    """Правый край окна — это когда сводку собрали."""
    keyboard = keyboards.under_digest(-100, 24)
    stamp = int(keyboard.inline_keyboard[1][0].callback_data.split(":")[3])
    assert abs(stamp - time.time()) < 5


def test_callback_влезает_в_лимит_телеграма():
    """Шестьдесят четыре байта — предел, за ним Telegram кнопку не примет."""
    keyboard = keyboards.under_digest(-1001234567890123, 336)
    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_кнопка_спросить_запоминает_чат(bot, user, state):
    callback = FakeCallback(bot, "ask:-100")

    await commands.on_ask_this_chat(callback, state)

    assert (await state.get_data())["chat_id"] == -100
    assert await state.get_state() == commands.Asking.question.state
    assert "5 «З» класс" in callback.message.replies[0]


async def test_вопрос_после_кнопки_идёт_только_в_этот_чат(bot, user, state, asked):
    await commands.on_ask_this_chat(FakeCallback(bot, "ask:-200"), state)
    await commands.on_free_question(FakeMessage(bot, "когда собрание?"), state)

    assert len(asked) == 1
    assert asked[0]["question"] == "когда собрание?"
    assert asked[0]["chat"].chat_id == -200


async def test_широкая_кнопка_спросить_не_наследует_чат(bot, user, state, asked):
    """Иначе чат от прошлой сводки молча сузил бы вопрос, заданный по всем."""
    await commands.on_ask_this_chat(FakeCallback(bot, "ask:-100"), state)
    await commands.on_button_ask(FakeMessage(bot), state)
    await commands.on_free_question(FakeMessage(bot, "когда собрание?"), state)

    assert asked[0]["chat"] is None


async def test_чат_не_живёт_дольше_одного_вопроса(bot, user, state, asked):
    await commands.on_ask_this_chat(FakeCallback(bot, "ask:-100"), state)
    await commands.on_free_question(FakeMessage(bot, "первый вопрос"), state)

    assert await state.get_state() is None, "состояние сбрасывается после ответа"
    assert (await state.get_data()) == {}


async def test_команда_вместо_вопроса_считается_отменой(bot, user, state, asked):
    await commands.on_ask_this_chat(FakeCallback(bot, "ask:-100"), state)
    message = FakeMessage(bot, "/help")
    await commands.on_free_question(message, state)

    assert asked == [], "иначе /help уезжает в модель и попадает в журнал вопросов"
    assert "Отменил вопрос" in message.replies[0]


async def test_кнопка_чужого_чата_честно_отказывает(bot, user, state, asked):
    callback = FakeCallback(bot, "ask:-999")

    await commands.on_ask_this_chat(callback, state)

    assert callback.alerted and callback.toast == texts.CHAT_GONE
    assert await state.get_state() is None


async def test_кнопка_после_смены_списка_чатов_отказывает(bot, user, state):
    """Сводка со старой кнопкой могла остаться в переписке навсегда."""
    db.set_chats(user.telegram_id, [(-300, "Новый чат")])
    callback = FakeCallback(bot, "ask:-100")

    await commands.on_ask_this_chat(callback, state)

    assert callback.toast == texts.CHAT_GONE


# --- Кнопка с фотографиями ---


async def test_кнопка_фото_несёт_свой_чат_и_окно(bot, user, shown):
    callback = FakeCallback(bot, "pics:-200:72:1757600000")

    await commands.on_show_photos(callback)

    assert shown[0]["chat"].chat_id == -200
    assert shown[0]["hours"] == 72
    assert shown[0]["until"] == 1_757_600_000


async def test_кнопка_первого_выпуска_продолжает_работать(bot, user, shown):
    """
    Сводки с трёхпольной кнопкой уже лежат у людей в переписке.

    Края окна в них нет — значит, край прежний, «сейчас». Сломать их нельзя:
    в переписке они остаются навсегда.
    """
    await commands.on_show_photos(FakeCallback(bot, "pics:-100:24"))

    assert shown[0]["hours"] == 24
    assert shown[0]["until"] is None


async def test_кнопка_фото_сразу_отвечает_телеграму(bot, user, shown):
    """Скачивание идёт минуту, а Telegram ждёт ответа секунды."""
    callback = FakeCallback(bot, "pics:-100:24:1757600000")
    await commands.on_show_photos(callback)
    assert callback.toast, "кнопка должна перестать крутиться до начала работы"


async def test_кнопка_фото_чужого_чата_отказывает(bot, user, shown):
    callback = FakeCallback(bot, "pics:-999:24:1757600000")
    await commands.on_show_photos(callback)
    assert callback.alerted and shown == []


async def test_неготовому_пользователю_кнопки_не_работают(bot, shown):
    db.create_user(500, "новичок")
    callback = FakeCallback(bot, "pics:-100:24:1757600000")
    await commands.on_show_photos(callback)
    assert callback.alerted and shown == []
