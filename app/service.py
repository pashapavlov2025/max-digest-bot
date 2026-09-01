"""Сборка и доставка сводки — общее для расписания и команд бота."""

import logging
from datetime import datetime, timezone

from aiogram import Bot

from . import db, digest, max_client
from .config import config
from .bot import texts

log = logging.getLogger(__name__)


async def send_digest(bot: Bot, user: db.User, hours: int = 24, quiet_if_empty: bool = False) -> bool:
    """Собирает сводку и отправляет её пользователю. False — если ничего не отправили."""
    if not user.phone or not user.chat_id:
        return False

    try:
        messages = await max_client.fetch_window(
            user.telegram_id, user.phone, user.chat_id, hours, config.max_messages
        )
    except Exception as exc:  # noqa: BLE001 — пользователю нужно знать, что сломалось
        log.warning("не смог прочитать чат для %s: %s", user.telegram_id, exc)
        await bot.send_message(user.telegram_id, texts.SESSION_BROKEN.format(error=exc))
        db.log_event(user.telegram_id, "fetch_failed", str(exc))
        return False

    if not messages:
        if not quiet_if_empty:
            await bot.send_message(user.telegram_id, texts.NOTHING.format(period=digest.period_label(hours)))
        return False

    try:
        result = await digest.build(messages, hours, provider=user.llm_provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("модель не справилась для %s: %s", user.telegram_id, exc)
        await bot.send_message(user.telegram_id, f"⚠️ Модель не ответила: {exc}")
        db.log_event(user.telegram_id, "llm_failed", str(exc))
        return False

    # Тихий день не тревожим уведомлением: смысл сервиса в экономии внимания
    if quiet_if_empty and digest.is_empty(result) and len(messages) < 5:
        db.log_event(user.telegram_id, "digest_skipped", "нечего сообщать")
        return False

    text = digest.render(result, hours, len(messages), user.chat_title)
    await send_long(bot, user.telegram_id, text)
    db.update_user(user.telegram_id, last_digest_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.log_event(user.telegram_id, "digest_sent", f"{len(messages)} сообщений")
    return True


async def answer_question(bot: Bot, user: db.User, question: str, days: int = 30) -> None:
    if not user.phone or not user.chat_id:
        return

    try:
        messages = await max_client.fetch_window(
            user.telegram_id, user.phone, user.chat_id, days * 24, config.max_messages
        )
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(user.telegram_id, texts.SESSION_BROKEN.format(error=exc))
        return

    if not messages:
        await bot.send_message(user.telegram_id, "В истории чата пока нет сообщений.")
        return

    try:
        reply = await digest.answer(question, messages, days, provider=user.llm_provider)
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(user.telegram_id, f"⚠️ Модель не ответила: {exc}")
        return

    await send_long(bot, user.telegram_id, f"<b>❓ {digest.esc(question)}</b>\n\n{digest.esc(reply)}")


LIMIT = 4096


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    """Telegram не принимает больше 4096 символов — режем по строкам."""
    if len(text) <= LIMIT:
        await bot.send_message(chat_id, text)
        return

    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > LIMIT:
            await bot.send_message(chat_id, chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        await bot.send_message(chat_id, chunk)
