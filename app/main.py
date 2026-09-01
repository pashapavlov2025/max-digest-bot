"""Точка входа: бот и планировщик в одном процессе."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import db, scheduler
from .bot import commands, handlers
from .config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("max-digest")


async def main() -> None:
    db.init()

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    # Команды идут первыми: иначе состояние онбординга перехватит /stop и /help
    dispatcher.include_router(commands.router)
    dispatcher.include_router(handlers.router)

    me = await bot.get_me()
    log.info("бот @%s запущен, провайдер модели: %s", me.username, config.llm_provider)

    asyncio.create_task(scheduler.run(bot))
    await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
