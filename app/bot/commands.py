"""Команды готового пользователя и админа."""

import logging
import secrets

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import crypto, db, max_client, service
from .handlers import Onboarding, show_chat_picker, time_keyboard
from ..config import config
from . import texts
from .keyboards import MAIN as MAIN_KEYBOARD

log = logging.getLogger(__name__)
router = Router()


class Asking(StatesGroup):
    question = State()


async def make_digest(message: Message, days: int) -> None:
    user = _require_user(message)
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
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
        await message.answer(texts.NOT_REGISTERED)
        return
    await message.answer(texts.ASK_QUESTION)
    await state.set_state(Asking.question)


@router.message(Asking.question)
async def on_free_question(message: Message, state: FSMContext) -> None:
    """Вопрос, заданный обычным текстом после нажатия кнопки."""
    await state.clear()
    user = _require_user(message)
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
        return
    question = (message.text or "").strip()
    if not question:
        return
    await message.answer(texts.WORKING, reply_markup=MAIN_KEYBOARD)
    await service.answer_question(message.bot, user, question)


@router.message(F.text == texts.BUTTON_SETTINGS)
async def on_button_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    await on_settings(message)


def _require_user(message: Message) -> db.User | None:
    user = db.get_user(message.from_user.id)
    return user if user and user.is_ready else None


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    keyboard = MAIN_KEYBOARD if _require_user(message) else None
    await message.answer(texts.HELP, reply_markup=keyboard)


@router.message(Command("summary"))
async def on_summary(message: Message, command: CommandObject) -> None:
    user = _require_user(message)
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
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
        await message.answer(texts.NOT_REGISTERED)
        return

    question = (command.args or "").strip()
    if not question:
        await message.answer("Задайте вопрос: <code>/q когда родительское собрание?</code>")
        return

    await message.answer(texts.WORKING)
    await service.answer_question(message.bot, user, question)


@router.message(Command("settings"))
async def on_settings(message: Message) -> None:
    user = _require_user(message)
    if user is None:
        await message.answer(texts.NOT_REGISTERED)
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
        await callback.message.answer(texts.NOT_REGISTERED)
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


# --- Админские команды ---


def _is_admin(message: Message) -> bool:
    return message.from_user.id in config.admin_ids


@router.message(Command("invite"))
async def on_invite(message: Message) -> None:
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return
    code = secrets.token_hex(4)
    db.add_invite(code, message.from_user.id)
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(
        f"Ссылка-приглашение, отправьте её другу:\n\n{link}\n\n"
        f"Она одноразовая. Если ссылки не работают, код можно ввести вручную: <code>{code}</code>"
    )


@router.message(Command("users"))
async def on_users(message: Message) -> None:
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return

    users = db.all_users()
    if not users:
        await message.answer("Пока никого.")
        return

    lines = [
        f"<code>{u.telegram_id}</code> @{u.username or '—'} · {u.state}"
        f"{' · пауза' if u.paused else ''} · {u.digest_time} · {len(u.chats)} чат(ов): {u.titles}"
        f"{f' · сбоев подряд {u.failures}' if u.failures else ''}"
        for u in users
    ]
    free = db.free_invites()
    lines.append(f"\nНеиспользованных кодов: {len(free)}")
    await message.answer("\n".join(lines))


@router.message(Command("provider"))
async def on_provider(message: Message, command: CommandObject) -> None:
    """/provider <telegram_id|all> <kimi|gigachat|default>"""
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return

    parts = (command.args or "").split()
    if len(parts) != 2 or parts[1] not in {"kimi", "gigachat", "default"}:
        await message.answer("Формат: <code>/provider all kimi</code> или <code>/provider 12345 gigachat</code>")
        return

    target, provider = parts
    value = None if provider == "default" else provider
    targets = db.all_users() if target == "all" else [u for u in db.all_users() if str(u.telegram_id) == target]
    for user in targets:
        db.update_user(user.telegram_id, llm_provider=value)
    await message.answer(f"Обновлено пользователей: {len(targets)} → {provider}")


@router.message(Command("health"))
async def on_health(message: Message) -> None:
    """Кто сломался и на чём. Без неё о чужих бедах узнаёшь только от самого человека."""
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return

    users = db.all_users()
    broken = [u for u in users if u.failures]
    ready = [u for u in users if u.is_ready and not u.paused]

    lines = [
        f"Пользователей: {len(users)}, работают: {len(ready)}, чатов всего: {sum(len(u.chats) for u in users)}"
    ]
    if not broken:
        lines.append("\nСбоев нет.")
    else:
        lines.append("\n<b>Сбоят:</b>")
        for u in broken:
            lines.append(
                f"• @{u.username or u.telegram_id} — подряд {u.failures}: <code>{(u.last_error or '')[:150]}</code>"
            )
    await message.answer("\n".join(lines))
