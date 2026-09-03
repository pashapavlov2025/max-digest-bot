"""
Чтение фотографий.

Замер на живом чате показал: из 27 фотографий за неделю 22 не имели вообще
никакого текста — букеты и снимки с первого сентября, читать там нечего.
Полезны те, у которых есть подпись или рядом задан вопрос: «по английскому
такую тетрадь надо?». Поэтому в модель уходят только они, а не всё подряд.

Первая же проверка на реальном чате вытащила таблицу с именами детей, датами
рождения и телефонами родителей. Отсюда главное правило промпта: содержимое
таких списков не пересказывать никогда.
"""

import asyncio
import base64
import logging
import re

import httpx

from . import db, llm
from .config import config

log = logging.getLogger(__name__)

SYSTEM = """Ты смотришь на фотографию из родительского чата школы и говоришь занятому родителю, есть ли на ней что-то, требующее действия.

- Расписание, объявление, домашнее задание, список покупок, реквизиты для оплаты, записка от учителя — перечисли суть коротко, одним-двумя предложениями. Даты, суммы и номера кабинетов называй точно.
- Снимок вещи, про которую спрашивают («такую тетрадь надо?», «эта форма?», «эти учебники?»), — опиши предмет так, чтобы его можно было опознать в магазине: что это, какое оформление, что написано на обложке.
- Фотографии детей, праздника, букетов, поздравления, мемы — ответь одним словом: НЕТ.
- Списки с личными данными не пересказывай никогда. Если на картинке таблица с именами детей, днями рождения, телефонами или адресами — так и напиши одной строкой: «список класса с контактами». Без имён, без номеров, без содержимого.
- Не выдумывай. Плохо видно — так и скажи: «не разобрать»."""

PROMPT = "Что на этой фотографии?"

# Слишком тяжёлые картинки не тянем: это почти всегда съёмка с телефона, а не документ
MAX_BYTES = 6 * 1024 * 1024
# Насколько позже вопрос ещё считается заданным про эту фотографию
QUESTION_WINDOW = 15 * 60
# Больше трёх одновременных запросов не нужно: сводка и так собирается минуту
PARALLEL = 3

# Модель не всегда отвечает голым «НЕТ» — бывает «НЕТ (это скриншот ошибки)».
# Отсекаем всё, что начинается с «нет»: описание, начатое с этого слова,
# полезным всё равно не бывает.
NOTHING = re.compile(r"^\W*нет\b", re.IGNORECASE)
# Описание уходит в сводку внутри квадратных скобок: разметка и переносы там лишние
MARKUP = re.compile(r"[*#`]+")
MAX_NOTE = 400


def _tidy(note: str) -> str:
    """Приводит ответ модели к одной строке без разметки."""
    note = MARKUP.sub("", note)
    note = " ".join(note.split())
    if len(note) > MAX_NOTE:
        note = note[:MAX_NOTE].rsplit(" ", 1)[0] + "…"
    return note.strip()


def _choose(messages: list[dict]) -> list[int]:
    """
    Какие фотографии стоит посмотреть.

    Два признака: своя подпись — или вопрос следом. Подпись почти всегда
    объясняет, зачем картинку прислали, а вопрос вроде «это технология или
    ин.яз?» означает, что на картинку смотрят все остальные.

    Вопрос привязываем к ближайшей предыдущей фотографии и только к ней:
    иначе один вопрос вытягивает на просмотр всю пачку праздничных снимков,
    которая шла перед ним.
    """
    picked = {index for index, message in enumerate(messages)
              if message.get("photos") and message.get("caption")}

    last_photo: int | None = None
    for index, message in enumerate(messages):
        if message.get("photos"):
            last_photo = index
        elif "?" in message["text"] and last_photo is not None:
            if message["time"] - messages[last_photo]["time"] <= QUESTION_WINDOW:
                picked.add(last_photo)
            last_photo = None

    return sorted(picked)


async def _describe(telegram_id: int, photo_id: int, url: str) -> str:
    """Одно описание. Пустая строка означает «смотреть не на что»."""
    cached = db.get_photo_note(telegram_id, photo_id)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as web:
            response = await web.get(url)
        if response.status_code != 200 or len(response.content) > MAX_BYTES:
            return ""
        image = base64.b64encode(response.content).decode()
        kind = response.headers.get("content-type", "image/jpeg").split(";")[0]
    except Exception as exc:  # noqa: BLE001 — картинка не обязана быть доступной
        log.info("картинку не скачал: %s", exc)
        return ""

    payload = {
        "model": config.vision_model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{image}"}},
                    {"type": "text", "text": PROMPT},
                ],
            },
        ],
    }
    if config.vision_model.startswith("kimi-k2"):
        payload["thinking"] = {"type": "disabled"}

    try:
        async with httpx.AsyncClient(timeout=180) as web:
            response = await web.post(
                f"{config.kimi_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {config.kimi_api_key}"},
                json=payload,
            )
        if response.status_code != 200:
            log.warning("модель не посмотрела картинку: %s %s", response.status_code, response.text[:200])
            return ""
        body = response.json()
        llm.record(body.get("usage"))
        note = body["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 — сводка важнее одной картинки
        log.warning("сбой при чтении картинки: %s", exc)
        return ""

    note = _tidy(note)
    if NOTHING.match(note) or len(note) < 3:
        note = ""
    # Пустой результат тоже помним: незачем платить дважды за один и тот же букет
    db.save_photo_note(telegram_id, photo_id, note)
    return note


async def enrich(telegram_id: int, messages: list[dict]) -> int:
    """
    Дописывает к сообщениям с фотографиями то, что на них видно.

    Меняет messages на месте. Возвращает число прочитанных картинок.
    """
    if not config.vision_enabled:
        return 0

    targets = [messages[index] for index in _choose(messages)][: config.vision_max_photos]
    if not targets:
        return 0

    gate = asyncio.Semaphore(PARALLEL)

    async def work(message: dict) -> None:
        photo_id, url = message["photos"][0]
        async with gate:
            note = await _describe(telegram_id, photo_id, url)
        if note:
            message["text"] = f"{message['text']} [на фото: {note}]"

    await asyncio.gather(*(work(message) for message in targets))
    read = sum(1 for message in targets if "[на фото:" in message["text"])
    log.info("посмотрел картинок: %s из %s подходящих", read, len(targets))
    return read
