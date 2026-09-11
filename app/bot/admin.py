"""
Команды админа: кто подключён, что у людей происходит и сколько это стоит.

Отдельно от пользовательских: аудитория другая, и в общем файле эти две
половины только мешали друг другу читаться. Роутер свой — он подключается
рядом с остальными в `app/main.py`.
"""

import logging
import secrets

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import crypto, db, service
from ..config import config
from . import texts

log = logging.getLogger(__name__)
router = Router(name="admin")


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


def _where(user: db.User) -> str:
    """
    Место в воронке по фактам, а не по колонке `state`.

    Колонка откатывается на «ждёт код» при каждом запросе кода и потом врёт
    про тех, кто вошёл: у застрявшего друга сессия MAX лежала на диске, а
    `/users` показывал его на шаге ввода номера.
    """
    if user.is_ready:
        return "работает"
    if user.chats:
        return "выбрал чаты, не назначил время"
    if crypto.has_session(user.telegram_id):
        return "вошёл, не выбрал чат"
    if user.phone:
        return "ввёл номер, ждёт код"
    return "не начал подключение MAX"


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
        f"<code>{u.telegram_id}</code> @{u.username or '—'} — {_where(u)}"
        + (f", {len(u.chats)} чат(ов): {u.titles}, сводка в {u.digest_time}" if u.is_ready else "")
        + (" · на паузе" if u.paused else "")
        + (f" · сбоев подряд {u.failures}" if u.failures else "")
        for u in users
    ]
    free = db.free_invites()
    lines.append(f"\nНеиспользованных кодов: {len(free)}")
    await message.answer("\n".join(lines))


# Больше двух сотен строк Telegram всё равно порежет на простыни
MAX_EVENTS = 200

# События словами: «login_failed» админу ни о чём не говорит
EVENT_NAMES = {
    "invite_used": "принял приглашение",
    "code_requested": "MAX выслал код",
    "login_failed": "вход не удался",
    "logged_in": "вошёл в MAX",
    "chats_failed": "не смог прочитать список чатов",
    "chats_set": "выбрал чаты",
    "stopped": "удалил свои данные",
    "digest_sent": "ушла сводка",
    "digest_quiet": "тихий день, только напоминание",
    "digest_skipped": "сводки не было, нечего сообщать",
    "morning_sent": "ушло утреннее напоминание",
    "burst_digest": "внеочередная сводка на всплеске",
    "question_asked": "спросил у бота",
    "photos_sent": "переслал фото из чата",
}


@router.message(Command("events"))
async def on_events(message: Message, command: CommandObject) -> None:
    """
    Что происходило у людей: /events, /events 50, /events 12345.

    Нужна ровно для случая «друг говорит, что не пришёл код»: видно, дошёл ли
    запрос до MAX, какой длины код он выслал и сколько попыток оставил.
    """
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return

    limit, who = 30, None
    for part in (command.args or "").split():
        if not part.isdigit():
            continue
        # Числа до двух сотен — это «сколько показать», всё крупнее — telegram_id
        value = int(part)
        if value <= MAX_EVENTS:
            limit = max(value, 1)
        else:
            who = value

    events = db.recent_events(limit, who)
    if not events:
        await message.answer("Событий нет." if who is None else f"У {who} событий нет.")
        return

    lines = [f"<b>Последние события</b>{'' if who is None else f' — {who}'}", ""]
    for event in events:
        name = EVENT_NAMES.get(event["kind"], event["kind"])
        detail = f" — <code>{texts.quote((event['detail'] or '')[:120])}</code>" if event["detail"] else ""
        # Имя, если оно есть: @1234567 читается как ник, а это id
        who_said = f"@{event['username']}" if event["username"] else str(event["telegram_id"] or "служба")
        lines.append(f"{event['at']} · {who_said}: {name}{detail}")

    await service.send_long(message.bot, message.chat.id, "\n".join(lines))


def _money(prompt: int, completion: int) -> str:
    """Деньги показываем, только если цена задана: выдуманная цифра хуже её отсутствия."""
    if not (config.price_in or config.price_out):
        return ""
    total = prompt / 1_000_000 * config.price_in + completion / 1_000_000 * config.price_out
    return f" ≈ ${total:.2f}"


@router.message(Command("stats"))
async def on_stats(message: Message, command: CommandObject) -> None:
    """Кто сколько сжёг токенов. /stats 7 — за неделю, по умолчанию за месяц."""
    if not _is_admin(message):
        await message.answer(texts.ADMIN_ONLY)
        return

    try:
        days = min(max(int((command.args or "30").split()[0]), 1), 180)
    except (ValueError, IndexError):
        days = 30

    people = db.usage_report(days)
    if not people:
        await message.answer(f"За {days} дн. обращений к модели не было.")
        return

    lines = [f"<b>Расход за {days} дн.</b>", ""]
    total_in = total_out = 0
    for row in people:
        prompt, completion = row["prompt"] or 0, row["completion"] or 0
        total_in += prompt
        total_out += completion
        lines.append(
            f"@{row['username'] or row['telegram_id']} — операций {row['operations']}, "
            f"обращений {row['calls']}\n    токенов: {prompt:,} на вход, {completion:,} на выход"
            f"{_money(prompt, completion)}".replace(",", " ")
        )

    lines += ["", "<b>По видам работы</b>"]
    names = {"digest": "сводки", "question": "вопросы", "morning": "утренние напоминания"}
    for row in db.usage_by_kind(days):
        prompt, completion = row["prompt"] or 0, row["completion"] or 0
        lines.append(
            f"• {names.get(row['kind'], row['kind'])}: {row['operations']} шт., "
            f"{prompt + completion:,} токенов".replace(",", " ")
        )

    lines += ["", f"<b>Всего:</b> {total_in + total_out:,} токенов{_money(total_in, total_out)}".replace(",", " ")]
    await service.send_long(message.bot, message.chat.id, "\n".join(lines))


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
                f"• @{u.username or u.telegram_id} — подряд {u.failures}: "
                f"<code>{texts.quote((u.last_error or '')[:150])}</code>"
            )
    await message.answer("\n".join(lines))
