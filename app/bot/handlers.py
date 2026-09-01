"""Диалог с пользователем: онбординг и команды."""

import asyncio
import logging
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .. import crypto, db, digest, max_client
from ..config import config
from . import texts

log = logging.getLogger(__name__)
router = Router()

# Незавершённые входы в MAX: живут в памяти, пока человек вводит код и пароль
logins: dict[int, max_client.LoginFlow] = {}
login_dirs: dict[int, Path] = {}


class Onboarding(StatesGroup):
    invite = State()
    password_hint = State()
    phone = State()
    code = State()
    max_password = State()
    chat = State()
    time = State()


def _yes_keyboard(text: str, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]])


def _time_keyboard() -> InlineKeyboardMarkup:
    options = ["18:00", "20:00", "21:00", "22:00"]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=f"time:{t}") for t in options]]
    )


async def _cleanup_login(telegram_id: int) -> None:
    flow = logins.pop(telegram_id, None)
    if flow:
        await flow.close()
    work_dir = login_dirs.pop(telegram_id, None)
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Онбординг ---


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _cleanup_login(message.from_user.id)

    user = db.get_user(message.from_user.id)
    if user and user.is_ready:
        await message.answer(texts.HELP)
        return

    if user is None:
        await message.answer(texts.START_LOCKED)
        await state.set_state(Onboarding.invite)
        return

    # Пользователь есть, но подключение не доведено до конца — продолжаем с начала
    await message.answer(texts.WELCOME)
    await message.answer(texts.NEED_PASSWORD, reply_markup=_yes_keyboard("Пароль установил", "pw:done"))
    await state.set_state(Onboarding.password_hint)


@router.message(Onboarding.invite)
async def on_invite(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not db.use_invite(code, message.from_user.id):
        await message.answer(texts.BAD_INVITE)
        return

    db.create_user(message.from_user.id, message.from_user.username)
    db.log_event(message.from_user.id, "invite_used", code)

    await message.answer(texts.WELCOME)
    await message.answer(texts.NEED_PASSWORD, reply_markup=_yes_keyboard("Пароль установил", "pw:done"))
    await state.set_state(Onboarding.password_hint)


@router.callback_query(F.data == "pw:done")
async def on_password_ready(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer(texts.ASK_PHONE, reply_markup=keyboard)
    await state.set_state(Onboarding.phone)


@router.message(Onboarding.phone)
async def on_phone(message: Message, state: FSMContext) -> None:
    raw = message.contact.phone_number if message.contact else (message.text or "")
    phone = re.sub(r"[^\d+]", "", raw)
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    if not re.fullmatch(r"\+\d{10,15}", phone):
        await message.answer(texts.BAD_PHONE)
        return

    await message.answer("Запрашиваю код у MAX…", reply_markup=ReplyKeyboardRemove())

    work_dir = Path(tempfile.mkdtemp(prefix=f"login-{message.from_user.id}-"))
    flow = max_client.LoginFlow(telegram_id=message.from_user.id, phone=phone, work_dir=work_dir)
    logins[message.from_user.id] = flow
    login_dirs[message.from_user.id] = work_dir

    await flow.start()
    if not await flow.wait_for_code_prompt():
        error = flow.error or "MAX не прислал код"
        await _cleanup_login(message.from_user.id)
        await message.answer(texts.LOGIN_FAILED.format(error=error))
        await state.clear()
        return

    db.update_user(message.from_user.id, phone=phone, state="connecting")
    await message.answer(texts.ASK_CODE.format(phone=phone))
    await state.set_state(Onboarding.code)


@router.message(Onboarding.code)
async def on_code(message: Message, state: FSMContext) -> None:
    flow = logins.get(message.from_user.id)
    if flow is None:
        await message.answer(texts.LOGIN_FAILED.format(error="сессия входа потерялась"))
        await state.clear()
        return

    code = re.sub(r"\D", "", message.text or "")
    if len(code) < 4:
        await message.answer("Нужен код из SMS — только цифры.")
        return

    await flow.submit_code(code)
    # MAX либо сразу пускает, либо спрашивает пароль — ждём, что случится первым
    for _ in range(40):
        if flow.needs_password():
            await message.answer(texts.ASK_MAX_PASSWORD)
            await state.set_state(Onboarding.max_password)
            return
        if flow.task and flow.task.done():
            break
        await asyncio.sleep(0.5)

    await _finish_login(message, state)


@router.message(Onboarding.max_password)
async def on_max_password(message: Message, state: FSMContext) -> None:
    flow = logins.get(message.from_user.id)
    if flow is None:
        await message.answer(texts.LOGIN_FAILED.format(error="сессия входа потерялась"))
        await state.clear()
        return

    await flow.submit_password((message.text or "").strip())
    # Пароль в переписке лучше не оставлять
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — не критично, если Telegram не дал удалить
        pass

    await _finish_login(message, state)


async def _finish_login(message: Message, state: FSMContext) -> None:
    flow = logins.get(message.from_user.id)
    assert flow is not None

    try:
        await flow.finish()
    except Exception as exc:  # noqa: BLE001 — текст ошибки нужен пользователю
        error = flow.error or str(exc)
        await _cleanup_login(message.from_user.id)
        await message.answer(texts.LOGIN_FAILED.format(error=error))
        await state.clear()
        return

    session_file = flow.work_dir / crypto.SESSION_FILE
    if not session_file.exists():
        await _cleanup_login(message.from_user.id)
        await message.answer(texts.LOGIN_FAILED.format(error="MAX не отдал сессию"))
        await state.clear()
        return

    crypto.store_session(message.from_user.id, session_file)
    phone = flow.phone
    await _cleanup_login(message.from_user.id)
    db.update_user(message.from_user.id, state="choosing_chat")
    db.log_event(message.from_user.id, "logged_in")

    await message.answer("Читаю список чатов…")
    try:
        chats = await max_client.list_chats(message.from_user.id, phone)
    except Exception as exc:  # noqa: BLE001
        await message.answer(texts.SESSION_BROKEN.format(error=exc))
        await state.clear()
        return

    if not chats:
        await message.answer(texts.NO_CHATS)
        await state.clear()
        return

    await show_chat_picker(message, state, chats, texts.CHOOSE_CHAT)
    await state.set_state(Onboarding.chat)


def describe_chat(chat: dict, timezone: str) -> str:
    """Приметы чата: по ним человек отличает похожие названия."""
    marks = []
    if chat["participants"]:
        marks.append(f"{chat['participants']} чел.")
    if chat["last_event"]:
        seconds = chat["last_event"] / 1000 if chat["last_event"] > 10**12 else chat["last_event"]
        moment = datetime.fromtimestamp(seconds, ZoneInfo(timezone))
        today = datetime.now(ZoneInfo(timezone)).date()
        if moment.date() == today:
            marks.append(f"писали сегодня в {moment:%H:%M}")
        else:
            marks.append(f"последнее {moment:%d.%m}")
    return " · ".join(marks) or "нет данных"


async def show_chat_picker(message: Message, state: FSMContext, chats: list[dict], header: str) -> None:
    """Показывает пронумерованный список с приметами и кнопки-цифры под ним."""
    shown = chats[:8]
    await state.update_data(
        chats={str(c["id"]): c["title"] for c in chats},
        all_chats=chats,
    )

    lines = [header, ""]
    for number, chat in enumerate(shown, 1):
        lines.append(f"<b>{number}. {chat['title']}</b>\n    <i>{describe_chat(chat, config.timezone)}</i>")

    buttons = [
        InlineKeyboardButton(text=str(number), callback_data=f"chat:{chat['id']}")
        for number, chat in enumerate(shown, 1)
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Onboarding.chat)
async def on_chat_search(message: Message, state: FSMContext) -> None:
    """В состоянии выбора текст трактуем как поиск по названию."""
    query = (message.text or "").strip()
    if not query:
        return

    data = await state.get_data()
    chats = data.get("all_chats") or []
    found = [c for c in chats if query.lower() in c["title"].lower()]

    if found:
        await show_chat_picker(message, state, found, texts.CHAT_FOUND.format(query=query))
    else:
        await show_chat_picker(message, state, chats, texts.CHAT_SEARCH_EMPTY.format(query=query))


@router.callback_query(F.data.startswith("chat:"))
async def on_chat_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    chat_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    title = (data.get("chats") or {}).get(chat_id, "чат")
    chat_id = int(chat_id)
    db.update_user(callback.from_user.id, chat_id=chat_id, chat_title=title)
    await callback.message.answer(texts.CHOOSE_TIME.format(title=title), reply_markup=_time_keyboard())
    await state.set_state(Onboarding.time)


@router.callback_query(F.data.startswith("time:"))
async def on_time_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _save_time(callback.from_user.id, callback.data.split(":", 1)[1], callback.message, state)


@router.message(Onboarding.time)
async def on_time_typed(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
        await message.answer("Нужно время в формате <code>21:30</code>.")
        return
    await _save_time(message.from_user.id, value, message, state)


async def _save_time(telegram_id: int, value: str, message: Message, state: FSMContext) -> None:
    hours, minutes = value.split(":")
    normalized = f"{int(hours):02d}:{minutes}"
    db.update_user(telegram_id, digest_time=normalized, state="ready")
    user = db.get_user(telegram_id)
    await message.answer(texts.DONE.format(time=normalized, title=user.chat_title if user else "чат"))
    await state.clear()
