"""Диалог с пользователем: онбординг и команды."""

import asyncio
import logging
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
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
from .keyboards import MAIN as MAIN_KEYBOARD

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


def time_keyboard() -> InlineKeyboardMarkup:
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
async def on_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    await _cleanup_login(message.from_user.id)

    user = db.get_user(message.from_user.id)
    if user and user.is_ready:
        await message.answer(texts.HELP, reply_markup=MAIN_KEYBOARD)
        return

    if user is None:
        # Приглашение обычно приходит ссылкой t.me/бот?start=код — код тогда уже здесь
        code = (command.args or "").strip()
        if code and db.use_invite(code, message.from_user.id):
            db.create_user(message.from_user.id, message.from_user.username)
            db.log_event(message.from_user.id, "invite_used", code)
            await message.answer(texts.WELCOME)
            await message.answer(texts.NEED_PASSWORD, reply_markup=_yes_keyboard("Пароль установил", "pw:done"))
            await state.set_state(Onboarding.password_hint)
            return

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


# Больше десятка вариантов человек уже не разглядывает — для остальных есть поиск
MAX_SHOWN = 10


def _picker_view(shown: list[dict], picked: dict[str, str], header: str):
    """Список чатов с галочками и кнопки-номера под ним."""
    lines = [header, ""]
    for number, chat in enumerate(shown, 1):
        mark = "✅ " if str(chat["id"]) in picked else ""
        lines.append(
            f"<b>{mark}{number}. {chat['title']}</b>\n    <i>{describe_chat(chat, config.timezone)}</i>"
        )

    buttons = [
        InlineKeyboardButton(
            text=("✅ " if str(chat["id"]) in picked else "") + str(number),
            callback_data=f"chat:{chat['id']}",
        )
        for number, chat in enumerate(shown, 1)
    ]
    rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    rows.append(
        [
            InlineKeyboardButton(
                text=f"Готово · выбрано {len(picked)}" if picked else "Готово",
                callback_data="chats:done",
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_chat_picker(
    message: Message,
    state: FSMContext,
    chats: list[dict],
    header: str,
    picked: dict[str, str] | None = None,
    mode: str | None = None,
) -> None:
    """
    Показывает чаты с возможностью отметить несколько.

    Отмеченное живёт в состоянии диалога и переживает поиск: человек может
    найти чат первоклассника, отметить, потом поискать секцию и отметить её же.
    """
    data = await state.get_data()
    shown = chats[:MAX_SHOWN]
    titles = {str(chat["id"]): chat["title"] for chat in chats}
    titles.update(data.get("titles") or {})

    if picked is None:
        picked = data.get("picked") or {}

    await state.update_data(
        all_chats=data.get("all_chats") or chats,
        shown=shown,
        titles=titles,
        picked=picked,
        header=header,
        # Поиск не должен сбрасывать, откуда пришли: из онбординга или из настроек
        mode=mode or data.get("mode") or "onboarding",
    )
    text, keyboard = _picker_view(shown, picked, header)
    await message.answer(text, reply_markup=keyboard)


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
async def on_chat_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажатие на номер добавляет чат в набор или убирает из него."""
    data = await state.get_data()
    picked = dict(data.get("picked") or {})
    key = callback.data.split(":", 1)[1]
    title = (data.get("titles") or {}).get(key, "чат")

    if key in picked:
        picked.pop(key)
        await callback.answer(f"Убрал «{title}»")
    else:
        picked[key] = title
        await callback.answer(f"Добавил «{title}»")

    await state.update_data(picked=picked)
    text, keyboard = _picker_view(data.get("shown") or [], picked, data.get("header") or texts.CHOOSE_CHAT_AGAIN)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001 — Telegram ругается, если текст не изменился
        pass


@router.callback_query(F.data == "chats:done")
async def on_chats_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    picked: dict[str, str] = data.get("picked") or {}
    if not picked:
        await callback.answer(texts.PICKED_NONE, show_alert=True)
        return

    await callback.answer()
    db.set_chats(callback.from_user.id, [(int(key), title) for key, title in picked.items()])
    db.log_event(callback.from_user.id, "chats_set", ", ".join(picked.values()))
    titles = ", ".join(picked.values())

    if data.get("mode") == "settings":
        await state.clear()
        await callback.message.answer(texts.CHATS_SAVED.format(title=titles), reply_markup=MAIN_KEYBOARD)
        return

    await callback.message.answer(texts.CHOOSE_TIME.format(title=titles), reply_markup=time_keyboard())
    await state.set_state(Onboarding.time)


@router.callback_query(F.data.startswith("time:"))
async def on_time_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _save_time(callback.from_user.id, callback.data.split(":", 1)[1], callback.message, state)


@router.callback_query(F.data.startswith("mtime:"))
async def on_morning_time(callback: CallbackQuery, state: FSMContext) -> None:
    """Время утреннего напоминания — отдельной кнопкой, без диалога."""
    await callback.answer()
    value = callback.data.split(":", 1)[1]
    if value == "off":
        db.update_user(callback.from_user.id, morning=0)
        await callback.message.answer(texts.MORNING_OFF, reply_markup=MAIN_KEYBOARD)
        return
    db.update_user(callback.from_user.id, morning=1, morning_time=value)
    await callback.message.answer(texts.MORNING_ON.format(time=value), reply_markup=MAIN_KEYBOARD)


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

    # Из настроек это правка одной строчки, а не завершение подключения
    if (await state.get_data()).get("mode") == "settings":
        await state.clear()
        await message.answer(f"Готово, сводка теперь в {normalized}.", reply_markup=MAIN_KEYBOARD)
        return

    await message.answer(
        texts.DONE.format(
            time=normalized,
            morning=user.morning_time if user else config.default_morning_time,
            title=user.titles if user else "чат",
        ),
        reply_markup=MAIN_KEYBOARD,
    )
    await state.clear()
