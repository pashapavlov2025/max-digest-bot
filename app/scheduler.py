"""
Всё, что происходит само: вечерняя сводка, утреннее напоминание
и внеочередная сводка при всплеске в чате.

Минутный цикл смотрит, кому подошло время. У каждого пользователя оно своё —
заодно это разносит обращения к MAX во времени, что полезно: несколько
аккаунтов, синхронно стучащихся с одного адреса, выглядят подозрительно.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from . import db, max_client, service
from .config import config

log = logging.getLogger(__name__)

# Окно замера активности и глубина внеочередной сводки
BURST_WINDOW_MINUTES = 60
BURST_DIGEST_HOURS = 3
# Пока замеров меньше, чем за сутки работы, судим только по абсолютному порогу
ENOUGH_SAMPLES = 24


async def run(bot: Bot) -> None:
    """Запускает оба цикла: минутный по расписанию и часовой по всплескам."""
    await asyncio.gather(_timetable(bot), _bursts(bot))


async def _timetable(bot: Bot) -> None:
    zone = ZoneInfo(config.timezone)
    done: dict[tuple[int, str], str] = {}

    while True:
        try:
            now = datetime.now(zone)
            stamp = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            for user in db.ready_users():
                if user.digest_time == stamp and done.get((user.telegram_id, "digest")) != today:
                    done[(user.telegram_id, "digest")] = today
                    _spawn("сводка", user, service.send_digest(bot, user, hours=24, quiet_if_empty=True))

                # Отметка об утреннем сообщении лежит в базе: перезапуск бота
                # не должен превращаться во второе напоминание за то же утро
                if (
                    user.morning
                    and user.morning_time == stamp
                    and user.last_morning != today
                    and done.get((user.telegram_id, "morning")) != today
                ):
                    done[(user.telegram_id, "morning")] = today
                    _spawn("утреннее напоминание", user, service.send_morning(bot, user))
        except Exception as exc:  # noqa: BLE001
            log.exception("сбой планировщика: %s", exc)

        await asyncio.sleep(60)


# Задачи держим за хвост: без ссылки сборщик мусора вправе убить их на полпути
_running: set[asyncio.Task] = set()


def _spawn(what: str, user: db.User, coro) -> None:
    """
    Отправляет сводку отдельной задачей.

    Не по стройности, а по делу: сбор одной сводки — это минуты, и если ждать
    её в общем цикле, у следующего пользователя минута его времени успеет пройти.
    """
    task = asyncio.create_task(_guarded(what, user, coro))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _guarded(what: str, user: db.User, coro) -> None:
    """Один сбой не должен ронять рассылку всем остальным."""
    # Небольшой случайный сдвиг, чтобы не ходить в MAX залпом
    await asyncio.sleep(random.uniform(0, 20))
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        log.exception("%s не ушла пользователю %s: %s", what, user.telegram_id, exc)


async def _bursts(bot: Bot) -> None:
    """
    Раз в час считает, сколько сообщений пришло в чат, и сравнивает с обычным.

    Модель здесь не нужна: всплеск виден по числу сообщений. Главный риск —
    спам, поэтому порогов два (абсолютный и кратный) и есть пауза после
    срабатывания, а ночью мы не тревожим вовсе.
    """
    if not config.burst_enabled:
        log.info("слежение за всплесками выключено")
        return

    # Не в начале часа: сводки и так уходят по круглым временам
    await asyncio.sleep(300)

    while True:
        try:
            if not _quiet_now():
                for user in db.ready_users():
                    if not user.bursts or not user.phone:
                        continue
                    try:
                        await _check_user(bot, user)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("замер активности не удался для %s: %s", user.telegram_id, exc)
                    await asyncio.sleep(random.uniform(1, 10))
        except Exception as exc:  # noqa: BLE001
            log.exception("сбой слежения за всплесками: %s", exc)

        await asyncio.sleep(3600)


def _quiet_now() -> bool:
    """Тихие часы обычно заданы через полночь — отсюда две ветки."""
    start, end = config.quiet_hours
    hour = datetime.now(ZoneInfo(config.timezone)).hour
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _cooling_down(user: db.User) -> bool:
    if not user.last_burst_at:
        return False
    try:
        last = datetime.fromisoformat(user.last_burst_at)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(hours=config.burst_cooldown_hours)


async def _check_user(bot: Bot, user: db.User) -> None:
    counts = await max_client.count_recent(
        user.telegram_id, user.phone, [chat.chat_id for chat in user.chats], BURST_WINDOW_MINUTES
    )

    for chat in user.chats:
        count = counts.get(chat.chat_id, 0)
        baseline, samples = db.activity_baseline(user.telegram_id, chat.chat_id)
        db.note_activity(user.telegram_id, chat.chat_id, count)

        if count < config.burst_min_messages:
            continue
        if samples >= ENOUGH_SAMPLES and count < baseline * config.burst_factor:
            continue
        if _cooling_down(user):
            log.info("всплеск в «%s» пропущен: пауза после прошлого", chat.title)
            continue

        log.info("всплеск в «%s»: %s сообщений за час при обычных %.1f", chat.title, count, baseline)
        await service.send_burst(bot, user, chat, hours=BURST_DIGEST_HOURS)
        # Дальше по этому пользователю уже пауза — остальные чаты подождут вечера
        return
