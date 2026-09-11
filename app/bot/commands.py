"""Команды готового пользователя. Админские живут рядом, в `admin.py`."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import crypto, db, digest, max_client, service
from .handlers import Onboarding, about_keyboard, show_chat_picker, time_keyboard
from ..config import config
from . import texts
from .keyboards import MAIN as MAIN_KEYBOARD

log = logging.getLogger(__name__)
router = Router(name="commands")


class Asking(StatesGroup):
    question = State()


async def make_digest(message: Message, days: int) -> None:
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return
    await message.answer(texts.WORKING, reply_markup=MAIN_KEYBOARD)
    await service.send_digest(message.bot, user, hours=days * 24)


@router.message(F.text == texts.BUTTON_DAY)
async def on_button_day(message: Message, state: FSMContext) -> None:
    await state.clear()
    await make_digest(message, 1)


@router.message(F.text == texts.BUTTON_THREE)
async def on_button_three(message: Message, state: FSMContext) -> None:
    await state.clear()
    await make_digest(message, 3)


@router.message(F.text == texts.BUTTON_ASK)
async def on_button_ask(message: Message, state: FSMContext) -> None:
    if _require_user(message) is None:
        await _deny(message)
        return
    # Данные чистим целиком: иначе чат от кнопки под прошлой сводкой молча
    # сузил бы этот вопрос, хотя человек спрашивает по всем
    await state.set_data({})
    await message.answer(texts.ASK_QUESTION)
    await state.set_state(Asking.question)


@router.callback_query(F.data.startswith("ask:"))
async def on_ask_this_chat(callback: CallbackQuery, state: FSMContext) -> None:
    """Вопрос по чату, под сводкой которого нажали кнопку."""
    user = db.get_user(callback.from_user.id)
    chat = _chat_of(user, callback.data.split(":", 1)[1])
    if chat is None:
        await callback.answer(texts.CHAT_GONE, show_alert=True)
        return

    await callback.answer()
    await state.set_data({"chat_id": chat.chat_id})
    await state.set_state(Asking.question)
    await callback.message.answer(texts.ASK_ABOUT_CHAT.format(title=texts.quote(chat.title)))


@router.callback_query(F.data.startswith("pics:"))
async def on_show_photos(callback: CallbackQuery) -> None:
    """Фотографии чата за то же окно, под которым стояла кнопка."""
    _, key, hours = callback.data.split(":")
    user = db.get_user(callback.from_user.id)
    chat = _chat_of(user, key)
    if chat is None:
        await callback.answer(texts.CHAT_GONE, show_alert=True)
        return

    # Отвечаем сразу: скачивание идёт минуту, а Telegram ждёт ответа секунды
    await callback.answer("Собираю фото…")
    await service.send_photos(callback.bot, user, chat, int(hours))


def _chat_of(user: db.User | None, key: str) -> db.Chat | None:
    """Чат пользователя по id из кнопки. None — список чатов с тех пор поменяли."""
    if user is None or not user.is_ready:
        return None
    return next((chat for chat in user.chats if str(chat.chat_id) == key), None)


@router.message(Asking.question)
async def on_free_question(message: Message, state: FSMContext) -> None:
    """Вопрос, заданный обычным текстом после нажатия кнопки."""
    data = await state.get_data()
    await state.clear()
    # Команда — это передумал, а не вопрос: иначе /help уезжает в модель
    # и попадает в журнал вопросов, что и случилось на живом боте
    if (message.text or "").startswith("/"):
        await message.answer("Отменил вопрос.", reply_markup=MAIN_KEYBOARD)
        return
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return
    question = (message.text or "").strip()
    if not question:
        return
    await message.answer(texts.WORKING, reply_markup=MAIN_KEYBOARD)
    # Чат кладёт кнопка под сводкой; без неё вопрос по-прежнему идёт по всем
    await service.answer_question(
        message.bot, user, question, chat=_chat_of(user, str(data.get("chat_id") or ""))
    )


@router.message(F.text == texts.BUTTON_SETTINGS)
async def on_button_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await on_settings(message)


def _require_user(message: Message) -> db.User | None:
    user = db.get_user(message.from_user.id)
    return user if user and user.is_ready else None


def _not_ready(user: db.User | None) -> str:
    """
    Отказ словами про то место, где человек застрял.

    Одинаковое «отправьте /start» на любой стадии однажды отправило друга
    на повторный вход в MAX, хотя аккаунт был подключён, а не хватало
    нажатия «Готово» под списком чатов.
    """
    if user is None:
        return texts.NOT_REGISTERED
    if user.chats:
        return texts.UNFINISHED_TIME.format(title=user.titles)
    if crypto.has_session(user.telegram_id):
        return texts.UNFINISHED_CHATS
    return texts.UNFINISHED_LOGIN


async def _deny(message: Message) -> None:
    """Ответ на команду, до которой человек ещё не дошёл."""
    await message.answer(_not_ready(db.get_user(message.from_user.id)))


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    keyboard = MAIN_KEYBOARD if _require_user(message) else None
    await message.answer(texts.with_about(texts.HELP, config.about_url), reply_markup=keyboard)


@router.message(Command("about"))
async def on_about(message: Message) -> None:
    """Страница о том, какой доступ получает бот. Спрашивают об этом первым делом."""
    if not config.about_url:
        await message.answer("Описание доступа не настроено на этом экземпляре.")
        return
    await message.answer(
        texts.ABOUT_LINE.format(url=config.about_url),
        reply_markup=about_keyboard(),
    )


@router.message(Command("summary"))
async def on_summary(message: Message, command: CommandObject) -> None:
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return

    try:
        days = min(max(int((command.args or "1").split()[0]), 1), 14)
    except (ValueError, IndexError):
        days = 1

    await message.answer(texts.WORKING)
    await service.send_digest(message.bot, user, hours=days * 24)


@router.message(Command("q"))
async def on_question(message: Message, command: CommandObject) -> None:
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return

    question = (command.args or "").strip()
    if not question:
        await message.answer("Задайте вопрос: <code>/q когда родительское собрание?</code>")
        return

    await message.answer(texts.WORKING)
    await service.answer_question(message.bot, user, question)


@router.message(F.text == texts.BUTTON_PLAN)
async def on_button_plan(message: Message, state: FSMContext) -> None:
    await state.clear()
    await on_plan(message)


@router.message(Command("plan"))
async def on_plan(message: Message) -> None:
    """Что бот запомнил на ближайшие две недели."""
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return

    rows = db.calendar_ahead(user.telegram_id)
    if not rows:
        await message.answer(texts.PLAN_EMPTY, reply_markup=MAIN_KEYBOARD)
        return

    await service.send_long(
        message.bot, message.chat.id, digest.render_ahead(rows, single_chat=len(user.chats) == 1)
    )


@router.message(Command("settings"))
async def on_settings(message: Message) -> None:
    user = _require_user(message)
    if user is None:
        await _deny(message)
        return

    morning = f"утром в {user.morning_time}" if user.morning else "выключено"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Изменить список чатов", callback_data="reconfigure")],
            [InlineKeyboardButton(text=f"🕗 Сводка в {user.digest_time}", callback_data="settime")],
            [InlineKeyboardButton(text=f"🌅 Утром: {morning}", callback_data="morning")],
            [
                InlineKeyboardButton(
                    text=f"🔥 Внеочередные сводки: {'вкл' if user.bursts else 'выкл'}",
                    callback_data="bursts",
                )
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Возобновить" if user.paused else "⏸ Поставить на паузу",
                    callback_data="toggle",
                )
            ],
        ]
    )

    chats = "\n".join(f"• {chat.title}" for chat in user.chats)
    await message.answer(
        f"<b>Чаты</b>\n{chats}\n\n"
        f"Сводка: <b>{user.digest_time}</b>, отдельно по каждому чату\n"
        f"Утреннее напоминание: <b>{morning}</b>\n"
        f"Внеочередные сводки при всплеске: <b>{'включены' if user.bursts else 'выключены'}</b>\n"
        f"Состояние: {'на паузе' if user.paused else 'работает'}",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "toggle")
async def on_toggle(callback: CallbackQuery) -> None:
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer()
        return
    db.update_user(user.telegram_id, paused=0 if user.paused else 1)
    await callback.answer()
    await callback.message.answer(texts.RESUMED if user.paused else texts.PAUSED)


@router.callback_query(F.data == "bursts")
async def on_bursts(callback: CallbackQuery) -> None:
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer()
        return
    db.update_user(user.telegram_id, bursts=0 if user.bursts else 1)
    await callback.answer()
    await callback.message.answer(texts.BURSTS_OFF if user.bursts else texts.BURSTS_ON)


@router.callback_query(F.data == "settime")
async def on_settime(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(mode="settings")
    await state.set_state(Onboarding.time)
    await callback.message.answer(
        "Во сколько присылать сводку? Кнопкой или своим временем в формате <code>21:30</code>.",
        reply_markup=time_keyboard(),
    )


@router.callback_query(F.data == "morning")
async def on_morning(callback: CallbackQuery) -> None:
    """Утреннее напоминание: только время или «выключить» — состояние не нужно."""
    await callback.answer()
    options = ["06:30", "07:00", "07:30", "08:00"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f"mtime:{t}") for t in options],
            [InlineKeyboardButton(text="Не напоминать по утрам", callback_data="mtime:off")],
        ]
    )
    await callback.message.answer(
        "Утром я коротко напомню о том, что запланировано на день, — если накануне "
        "в чате об этом писали. Во сколько?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "reconfigure")
async def on_reconfigure(callback: CallbackQuery, state: FSMContext) -> None:
    """Правка списка чатов без повторного входа в MAX — сессия уже есть."""
    await callback.answer()
    user = db.get_user(callback.from_user.id)
    if user is None or not user.phone:
        await callback.message.answer(_not_ready(user))
        return

    await callback.message.answer("Смотрю, какие есть чаты…")
    try:
        chats = await max_client.list_chats(user.telegram_id, user.phone)
    except Exception as exc:  # noqa: BLE001 — пользователю нужно знать причину
        await service.report_failure(callback.bot, user, exc)
        return

    if not chats:
        await callback.message.answer(texts.NO_CHATS)
        return

    # Уже подключённые показываем отмеченными: человек правит набор, а не собирает заново
    picked = {str(chat.chat_id): chat.title for chat in user.chats}
    await state.clear()
    await show_chat_picker(
        callback.message, state, chats, texts.CHOOSE_CHAT_AGAIN, picked=picked, mode="settings"
    )
    await state.set_state(Onboarding.chat)


@router.message(Command("stop"))
async def on_stop(message: Message) -> None:
    crypto.drop_session(message.from_user.id)
    db.delete_user(message.from_user.id)
    db.log_event(message.from_user.id, "stopped")
    await message.answer(texts.STOPPED)
