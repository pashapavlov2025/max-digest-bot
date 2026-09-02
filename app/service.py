"""Сборка и доставка сводок — общее для расписания и команд бота."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from . import db, digest, errors, max_client
from .config import config
from .bot import texts

log = logging.getLogger(__name__)

LIMIT = 4096
# Через столько неудач подряд считаем, что сломалось всерьёз, и зовём админа
ALARM_AT = 3


async def send_digest(bot: Bot, user: db.User, hours: int = 24, quiet_if_empty: bool = False) -> int:
    """
    Сводка по каждому чату отдельным сообщением.

    Отдельным — намеренно: у человека может быть чат первоклассника, чат
    старшего и секция, и сваленные в одну простыню они нечитаемы.

    Возвращает число отправленных сводок.
    """
    if not user.phone or not user.chats:
        return 0

    sent = 0
    for chat in user.chats:
        try:
            if await _digest_chat(bot, user, chat, hours, quiet_if_empty):
                sent += 1
        except Exception as exc:  # noqa: BLE001 — сбой на одном чате не отменяет остальные
            log.exception("сводка по чату %s не собралась: %s", chat.chat_id, exc)

    if sent:
        db.update_user(
            user.telegram_id,
            last_digest_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    return sent


async def _digest_chat(
    bot: Bot, user: db.User, chat: db.Chat, hours: int, quiet_if_empty: bool
) -> bool:
    announce = not quiet_if_empty  # человек ждёт ответа только когда попросил сам

    try:
        messages = await max_client.fetch_window(
            user.telegram_id, user.phone, chat.chat_id, hours, config.max_messages
        )
    except Exception as exc:  # noqa: BLE001
        await report_failure(bot, user, exc, where=chat.title, announce=announce)
        return False

    if not messages:
        db.note_success(user.telegram_id)
        if announce:
            await bot.send_message(
                user.telegram_id,
                texts.NOTHING.format(title=chat.title, period=digest.period_label(hours)),
            )
        return False

    try:
        result = await digest.build(messages, hours, provider=user.llm_provider)
    except Exception as exc:  # noqa: BLE001
        await report_failure(bot, user, exc, where=chat.title, announce=announce, kind_hint="llm")
        return False

    # Счётчик сбоев сбрасываем только здесь: чтение прошло, модель ответила.
    # Иначе сломанная модель каждый день считалась бы первым сбоем и каждый день
    # об этом сообщала.
    db.note_success(user.telegram_id)
    result["tomorrow"] = await _tomorrow_with_memory(user, chat, result)
    await _remember(user, chat, result)

    # Тихий день не тревожим уведомлением: смысл сервиса в экономии внимания
    if quiet_if_empty and digest.is_empty(result) and len(messages) < 5:
        db.log_event(user.telegram_id, "digest_skipped", f"{chat.title}: нечего сообщать")
        return False

    text = digest.render(result, hours, len(messages), chat.title)
    await send_long(bot, user.telegram_id, text)
    db.log_event(user.telegram_id, "digest_sent", f"{chat.title}: {len(messages)} сообщений")
    return True


def _tomorrow(zone: ZoneInfo) -> str:
    return (datetime.now(zone) + timedelta(days=1)).strftime("%Y-%m-%d")


async def _tomorrow_with_memory(user: db.User, chat: db.Chat, result: dict) -> list[dict]:
    """
    Блок «завтра» пополняется тем, что бот запомнил раньше.

    В живом чате про поднятие флага сказали один раз за десять дней до него.
    Сводка за сутки такое не увидит — она читает только вчерашнюю переписку.
    Календарь видит, поэтому накануне событие всё равно всплывёт.
    """
    zone = ZoneInfo(config.timezone)
    remembered = [
        {"when": item["when"], "what": item["what"]}
        for item in db.calendar_on(user.telegram_id, _tomorrow(zone), chat.chat_id)
    ]
    items = digest.tomorrow_items(result) + remembered
    if not items:
        return []
    return await digest.merge(items, provider=user.llm_provider)


async def _remember(user: db.User, chat: db.Chat, result: dict) -> None:
    """
    Складывает события в календарь, сводя каждую дату к итоговому плану.

    Склеиваем при записи, а не при чтении: иначе календарь копит формулировки.
    За неделю живого чата про линейку 1 сентября набралось одиннадцать строк —
    каждый день своими словами, и ключ по тексту такие повторы не ловит.
    """
    zone = ZoneInfo(config.timezone)
    tomorrow = _tomorrow(zone)

    fresh: dict[str, list[dict]] = {}
    for item in digest.dated_events(result):
        fresh.setdefault(item["date"], []).append({"when": item["time"], "what": item["what"]})
    for item in result.get("tomorrow") or []:
        fresh.setdefault(tomorrow, []).append({"when": item.get("when", ""), "what": item["what"]})

    for on_date, items in fresh.items():
        known = [
            {"when": row["when"], "what": row["what"]}
            for row in db.calendar_on(user.telegram_id, on_date, chat.chat_id)
        ]
        # Сводим всегда, когда строк больше одной. Дублировать друг друга успевают
        # и разделы одной сводки: событие с датой и пункт «на завтра» — это часто
        # одно и то же, записанное дважды.
        combined = items + known
        plan = await digest.merge(combined, provider=user.llm_provider) if len(combined) > 1 else combined
        db.replace_calendar(
            user.telegram_id,
            chat.chat_id,
            on_date,
            [{"time": entry.get("when", ""), "what": entry["what"]} for entry in plan],
        )
    if fresh:
        log.info("в календарь %s: дат %s", user.telegram_id, len(fresh))


async def send_morning(bot: Bot, user: db.User) -> bool:
    """
    Утреннее напоминание о том, что запланировано на сегодня.

    Берётся из календаря, а не из вчерашней сводки: про часть событий
    в чате писали неделю назад и с тех пор молчат.
    """
    zone = ZoneInfo(config.timezone)
    today = datetime.now(zone).strftime("%Y-%m-%d")
    rows = db.calendar_on(user.telegram_id, today)
    if not rows:
        db.update_user(user.telegram_id, last_morning=today)
        return False

    by_chat: dict[str, list[dict]] = {}
    for row in rows:
        by_chat.setdefault(row["title"], []).append({"when": row["when"], "what": row["what"]})

    blocks = [
        (title, await digest.merge(items, provider=user.llm_provider))
        for title, items in by_chat.items()
    ]
    await send_long(bot, user.telegram_id, digest.render_agenda(blocks, single_chat=len(user.chats) == 1))
    db.update_user(user.telegram_id, last_morning=today)
    db.log_event(user.telegram_id, "morning_sent", f"{sum(len(items) for _, items in blocks)} пунктов")
    return True


async def answer_question(bot: Bot, user: db.User, question: str, days: int = 30) -> None:
    """
    Ищет ответ во всех подключённых чатах.

    Чаты, где про это не писали, молчат: три вежливых «не нашёл» вместо
    одного ответа — худшее, что можно сделать с вопросом.
    """
    if not user.phone or not user.chats:
        return

    answers: list[tuple[str, str]] = []
    for chat in user.chats:
        try:
            messages = await max_client.fetch_window(
                user.telegram_id, user.phone, chat.chat_id, days * 24, config.max_messages
            )
        except Exception as exc:  # noqa: BLE001
            await report_failure(bot, user, exc, where=chat.title, announce=True)
            continue

        if not messages:
            continue

        try:
            reply = await digest.answer(question, messages, days, provider=user.llm_provider)
        except Exception as exc:  # noqa: BLE001
            await report_failure(bot, user, exc, where=chat.title, announce=True, kind_hint="llm")
            continue

        if not digest.is_nothing(reply):
            answers.append((chat.title, reply))

    if not answers:
        await bot.send_message(user.telegram_id, texts.ANSWER_EMPTY)
        return

    header = f"<b>❓ {digest.esc(question)}</b>"
    if len(answers) == 1 and len(user.chats) == 1:
        await send_long(bot, user.telegram_id, f"{header}\n\n{digest.esc(answers[0][1])}")
        return

    parts = [header]
    for title, reply in answers:
        parts.append(f"\n<b>{digest.esc(title)}</b>\n{digest.esc(reply)}")
    await send_long(bot, user.telegram_id, "\n".join(parts))


async def send_burst(bot: Bot, user: db.User, chat: db.Chat, hours: int = 3) -> bool:
    """
    Внеочередная сводка: в чате что-то случилось, ждать вечера незачем.

    Заголовок объясняет, почему сообщение пришло не по расписанию, — иначе
    человек решит, что бот сломался и шлёт что попало.
    """
    await bot.send_message(user.telegram_id, texts.BURST.format(title=digest.esc(chat.title)))
    sent = await _digest_chat(bot, user, chat, hours, quiet_if_empty=False)
    db.update_user(
        user.telegram_id,
        last_burst_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    db.log_event(user.telegram_id, "burst_digest", chat.title)
    return sent


# --- Живучесть ---


async def report_failure(
    bot: Bot,
    user: db.User,
    exc: BaseException,
    where: str = "",
    announce: bool = True,
    kind_hint: str | None = None,
) -> errors.Failure:
    """
    Объясняет сбой человеку и, если он затянулся, зовёт админа.

    По расписанию молчим о повторах: ежедневное «опять не смог» превращает
    сервис в источник раздражения. Первый раз сказали — дальше ждём действия.
    """
    failure = errors.classify(exc, kind_hint)
    count = db.note_failure(user.telegram_id, f"{where}: {failure.detail}" if where else failure.detail)
    log.warning("сбой у %s (%s, подряд %s): %s", user.telegram_id, failure.kind, count, failure.detail)
    db.log_event(user.telegram_id, f"failed_{failure.kind}", failure.detail)

    if announce or count == 1 or count % 7 == 0:
        place = f" по чату «{where}»" if where and len(user.chats) > 1 else ""
        try:
            await bot.send_message(user.telegram_id, texts.FAILED.format(place=place, advice=failure.advice))
        except Exception:  # noqa: BLE001 — человек мог заблокировать бота
            pass

    if count == ALARM_AT or (failure.fatal and count == 1):
        await notify_admins(
            bot,
            f"⚠️ У @{user.username or user.telegram_id} ({user.telegram_id}) сбой "
            f"«{failure.kind}» подряд {count} раз:\n<code>{digest.esc(failure.detail)}</code>",
        )

    return failure


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:  # noqa: BLE001 — админ тоже мог не начать диалог с ботом
            log.warning("не смог уведомить админа %s: %s", admin_id, exc)


# Начиная с этой заполненности предпочитаем разрезать по началу раздела
SECTION_FILL = 0.7


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    """
    Telegram не принимает больше 4096 символов — режем по строкам.

    Режем по возможности на границе раздела, а не посреди списка: сводка
    за неделю в лимит не влезает, и разрыв между «Наглядной геометрией»
    и заголовком «Решения» выглядит поломкой, а не длинным сообщением.
    """
    if len(text) <= LIMIT:
        await bot.send_message(chat_id, text)
        return

    chunk = ""
    for line in text.split("\n"):
        overflow = len(chunk) + len(line) + 1 > LIMIT
        section = line.startswith("<b>") and len(chunk) > LIMIT * SECTION_FILL
        if chunk and (overflow or section):
            await bot.send_message(chat_id, chunk.strip())
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk.strip():
        await bot.send_message(chat_id, chunk.strip())
