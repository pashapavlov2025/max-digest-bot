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
import time
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
    """Запускает три цикла: расписание, всплески и присмотр за входом."""
    await asyncio.gather(_timetable(bot), _bursts(bot), _watchdog(bot))


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


# --- Присмотр за входом ---
#
# Вход ломается тише всех остальных частей: у подключённых людей сводки идут,
# а новичок упирается в стену и пишет «код не приходит». Так и было в сентябре
# 2026, когда MAX перестал обслуживать версию клиента, зашитую в библиотеке:
# запрос кода принимался, код не отправлялся, в журнале ни одной ошибки.
# Поэтому проверяем сами и жалуемся админам, а не ждём жалобы от новичка.


async def _watchdog(bot: Bot) -> None:
    """
    Раз в сутки смотрит, не устарела ли версия клиента и доходят ли коды.

    Внешний try не для красоты: этот цикл живёт в одной `gather` со сводками,
    и упавшая проверка утащила бы за собой рассылку. Присмотр важен, но не
    настолько, чтобы ради него терять то, ради чего бот запущен.
    """
    try:
        if not config.watchdog_time:
            log.info("присмотр за входом выключен")
            return
        zone = ZoneInfo(config.timezone)
    except Exception as exc:  # noqa: BLE001
        log.exception("присмотр за входом не запустился: %s", exc)
        return
    done: str | None = None

    while True:
        try:
            now = datetime.now(zone)
            today = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") == config.watchdog_time and done != today:
                done = today
                await _check_version(bot)
                await _check_delivery(bot)
        except Exception as exc:  # noqa: BLE001
            log.exception("сбой присмотра за входом: %s", exc)

        await asyncio.sleep(60)


async def _check_version(bot: Bot) -> None:
    """
    Не отстали ли мы от живых версий MAX.

    Проверка дешёвая и без следов: сверяем, чем представляемся, со списком
    свежих сборок. Именно это отставание и сломало вход в сентябре.
    """
    try:
        newest = await max_client.newest_versions()
    except Exception as exc:  # noqa: BLE001 — каталог по сети, он может и не ответить
        log.warning("не смог узнать свежие версии MAX: %s", exc)
        return

    if max_client.APP_VERSION in newest:
        log.info("версия клиента %s в числе свежих", max_client.APP_VERSION)
        return

    versions = ", ".join(newest)
    db.log_event(None, "version_stale", f"{max_client.APP_VERSION}, свежие: {versions}")
    await service.notify_admins(
        bot,
        "⚠️ Версия клиента MAX устарела.\n\n"
        f"Мы представляемся {max_client.APP_VERSION}, а свежие сейчас: {versions}.\n\n"
        "Пока вход работает, но именно так он и сломался в прошлый раз: MAX "
        "перестаёт отправлять коды, не показывая ошибки. Стоит поднять "
        "APP_VERSION в app/max_client.py.",
    )


async def _check_delivery(bot: Bot) -> None:
    """
    Настоящая проверка: просим код себе и смотрим, дошёл ли он.

    Дёргаем нечасто — каждая проверка кладёт админу в MAX сообщение «кто-то
    пытается войти», и делать это ежедневно незачем. Зато она отвечает на
    вопрос, который иначе не проверить ничем: коды доходят или нет.
    """
    days = config.watchdog_canary_days
    if days <= 0:
        return

    last = db.last_event_at("delivery_ok") or db.last_event_at("delivery_failed")
    if last:
        try:
            when = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - when < timedelta(days=days):
                return
        except ValueError:
            pass

    user = next(
        (u for u in db.ready_users() if u.telegram_id in config.admin_ids and u.phone), None
    )
    if not user:
        log.info("проверять доставку кода не на ком: у админов нет подключённого MAX")
        return

    since_ms = time.time() * 1000
    request = await max_client.probe_code(user.phone)
    if request is None:
        db.log_event(user.telegram_id, "delivery_failed", "MAX не принял запрос кода")
        await service.notify_admins(bot, "⚠️ MAX не принял запрос кода — вход, похоже, сломан.")
        return

    # Доставка занимает секунды, но пусть у MAX будет запас
    await asyncio.sleep(45)

    try:
        arrived = await max_client.code_arrived(user.telegram_id, user.phone, since_ms)
    except Exception as exc:  # noqa: BLE001
        log.warning("не смог проверить доставку кода: %s", exc)
        return

    if arrived:
        db.log_event(user.telegram_id, "delivery_ok", str(request))
        log.info("проверка доставки кода: код дошёл")
        return

    db.log_event(user.telegram_id, "delivery_failed", str(request))
    await service.notify_admins(
        bot,
        "⚠️ Код входа не дошёл.\n\n"
        f"MAX принял запрос ({request}), но сообщение с кодом в мессенджере "
        "не появилось. Значит новые люди подключиться не смогут, хотя у всех "
        "подключённых сводки идут как обычно.\n\n"
        f"Первым делом стоит проверить версию клиента: сейчас {max_client.APP_VERSION}.",
    )
