"""
Рассылка сводок по расписанию.

Раз в минуту смотрим, кому подошло время. У каждого пользователя оно своё —
заодно это разносит обращения к MAX во времени, что полезно: несколько
аккаунтов, синхронно стучащихся с одного адреса, выглядят подозрительно.
"""

import asyncio
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from . import db, service
from .config import config

log = logging.getLogger(__name__)


async def run(bot: Bot) -> None:
    zone = ZoneInfo(config.timezone)
    sent_today: dict[int, str] = {}

    while True:
        try:
            now = datetime.now(zone)
            stamp = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            for user in db.ready_users():
                if user.digest_time != stamp or sent_today.get(user.telegram_id) == today:
                    continue

                sent_today[user.telegram_id] = today
                # Небольшой случайный сдвиг, чтобы не ходить в MAX залпом
                await asyncio.sleep(random.uniform(0, 20))
                try:
                    await service.send_digest(bot, user, hours=24, quiet_if_empty=True)
                except Exception as exc:  # noqa: BLE001 — один сбой не должен ронять рассылку
                    log.exception("сводка не ушла пользователю %s: %s", user.telegram_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("сбой планировщика: %s", exc)

        await asyncio.sleep(60)
