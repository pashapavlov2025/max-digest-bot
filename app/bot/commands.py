"""Команды готового пользователя и админа."""

import logging
import secrets

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import crypto, db, service
from ..config import config
from . import texts

log = logging.getLogger(__name__)
router = Router()


def _require_user(message: Message) -> db.User | None:
    user = db.get_user(message.from_user.id)
    return user if user and user.is_ready else None


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(texts.HELP)


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

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Возобновить" if user.paused else "Поставить на паузу", callback_data="toggle")],
            [InlineKeyboardButton(text="Сменить чат или время", callback_data="reconfigure")],
        ]
    )
    await message.answer(
        f"Чат: <b>{user.chat_title}</b>\nВремя сводки: <b>{user.digest_time}</b>\n"
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


@router.callback_query(F.data == "reconfigure")
async def on_reconfigure(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Отправьте /start — пройдём настройку заново, переподключать MAX не понадобится.")


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
    await message.answer(f"Код приглашения: <code>{code}</code>")


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
        f"{' · пауза' if u.paused else ''} · {u.digest_time} · {u.chat_title or '—'}"
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
