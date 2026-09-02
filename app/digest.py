"""
Сводка: промпт, разбор ответа модели и рендер в сообщение Telegram.

Структуру просим словами, а не через json_schema: её принимают не все
провайдеры, а разбор всё равно нужен свой.
"""

import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import llm
from .config import config

SYSTEM = """Ты — ассистент занятого родителя. Он не читает родительский чат школы вообще и полагается только на твою сводку.

Твоя задача — вытащить из болтовни то, что имеет практические последствия, и безжалостно выкинуть остальное.

Правила:
- Пиши по-русски, коротко, без воды и без вежливых вступлений.
- Никогда не выдумывай факты, суммы и даты. Если чего-то нет в переписке — ставь «—».
- Относительные даты («в пятницу», «завтра») переводи в конкретные числа, опираясь на текущую дату.
- Если несколько сообщений про одно и то же — объединяй в один пункт.
- Отменённое или изменённое позже по ходу переписки указывай в итоговом, актуальном виде.
- Поздравления, спасибо, стикеры, споры ни о чём — это шум, ему место только в поле noise.
- Не переноси в сводку чужие телефоны, адреса и прочие личные контакты: обмен ими — это шум.
- Пустой раздел — это нормально, оставь пустой список, не придумывай наполнение.

Вложения. Картинки и файлы ты не видишь — вместо них в переписке стоят пометки
в квадратных скобках: [фото], [файл «имя»], [опрос «вопрос»; варианты: ...].
Что с ними делать:
- Опрос — это почти всегда решение или его обсуждение. Разбирай его как обычное содержание: вопрос и варианты тебе видны.
- Если по переписке понятно, что на фото или в файле лежит нужное (расписание, список, домашнее задание), напиши об этом в разделе с действиями: «прислали расписание картинкой — посмотреть в чате за такое-то число». Не притворяйся, будто знаешь содержимое.
- Просто фотографии с праздника и прочие снимки без последствий — это шум."""

JSON_SHAPE = """Ответь строго одним JSON-объектом без пояснений вокруг:
{
  "headline": "одна строка — главное за период",
  "actions": [{"task": "что сделать", "deadline": "до 12.09 или —", "details": "уточнение или —"}],
  "money":   [{"what": "за что", "amount": "сумма или —", "deadline": "срок или —", "recipient": "кому или —"}],
  "events":  [{"when": "дата", "what": "что происходит"}],
  "decisions": ["до чего договорились"],
  "unanswered": ["открытые вопросы, где ждут ответа родителей"],
  "noise": "одной строкой: о чём был остальной трёп"
}
Все семь ключей обязательны. Пустой список — это [], пустая строка — "—"."""

QUESTION_SYSTEM = """Ты отвечаешь на вопросы родителя по переписке школьного родительского чата.

Как отвечать:
- Сразу давай готовый ответ своими словами, одним-тремя предложениями. Это главное.
- В конце добавь в скобках дату сообщения-источника, например «(из чата 28.08)».
- Никогда не пересказывай переписку списком и не приводи цитаты целиком — родитель просит ответ, а не выписку.
- Если в чате есть только косвенно связанное — скажи, что прямого ответа нет, и коротко приведи то, что есть.
- Если совсем ничего — «в чате про это не писали». Ничего не придумывай.
- Чужие телефоны и адреса не приводи, если только вопрос не был именно про контакт."""

EMPTY = {"—", "-", "", "нет", "не указано"}


def _filled(value: object) -> bool:
    return str(value).strip().lower() not in EMPTY


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def period_label(hours: int) -> str:
    if hours <= 24:
        return "последние сутки"
    return f"последние {round(hours / 24)} дн."


def transcript(messages: list[dict], tz: str) -> str:
    zone = ZoneInfo(tz)
    lines = []
    for message in messages:
        stamp = datetime.fromtimestamp(message["time"], zone).strftime("%d.%m %H:%M")
        lines.append(f"[{stamp}] {message['author']}: {message['text']}")
    return "\n".join(lines)


async def build(messages: list[dict], hours: int, provider: str | None = None) -> dict:
    zone = ZoneInfo(config.timezone)
    today = datetime.now(zone).strftime("%A, %d %B %Y")

    prompt = f"""Сегодня {today}.

Ниже переписка родительского чата за {period_label(hours)} ({len(messages)} сообщений). Сделай сводку.

--- НАЧАЛО ПЕРЕПИСКИ ---
{transcript(messages, config.timezone)}
--- КОНЕЦ ПЕРЕПИСКИ ---"""

    raw = await llm.complete(f"{SYSTEM}\n\n{JSON_SHAPE}", prompt, json_mode=True, provider=provider)
    return llm.extract_json(raw)


async def answer(question: str, messages: list[dict], days: int, provider: str | None = None) -> str:
    prompt = f"""Переписка за последние {days} дн.:

--- НАЧАЛО ПЕРЕПИСКИ ---
{transcript(messages, config.timezone)}
--- КОНЕЦ ПЕРЕПИСКИ ---

Вопрос: {question}"""
    return await llm.complete(QUESTION_SYSTEM, prompt, provider=provider)


def render(digest: dict, hours: int, message_count: int, chat_title: str | None = None) -> str:
    title = chat_title or "Родительский чат"
    lines = [f"<b>📋 {esc(title)} — {esc(period_label(hours))}</b>"]

    if digest.get("headline"):
        lines.append(esc(digest["headline"]))

    if digest.get("actions"):
        lines += ["", "<b>🔴 Требуется от меня</b>"]
        for item in digest["actions"]:
            tail = " · ".join(
                part
                for part in (
                    f"<b>{esc(item.get('deadline'))}</b>" if _filled(item.get("deadline")) else "",
                    esc(item.get("details")) if _filled(item.get("details")) else "",
                )
                if part
            )
            lines.append(f"• {esc(item.get('task'))}" + (f" — {tail}" if tail else ""))

    if digest.get("money"):
        lines += ["", "<b>💰 Деньги</b>"]
        for item in digest["money"]:
            tail = " · ".join(
                part
                for part in (
                    f"<b>{esc(item.get('amount'))}</b>" if _filled(item.get("amount")) else "",
                    esc(item.get("recipient")) if _filled(item.get("recipient")) else "",
                    esc(item.get("deadline")) if _filled(item.get("deadline")) else "",
                )
                if part
            )
            lines.append(f"• {esc(item.get('what'))}" + (f" — {tail}" if tail else ""))

    if digest.get("events"):
        lines += ["", "<b>📅 Даты и события</b>"]
        for item in digest["events"]:
            lines.append(f"• {esc(item.get('when'))} — {esc(item.get('what'))}")

    if digest.get("decisions"):
        lines += ["", "<b>✅ Решения</b>"]
        lines += [f"• {esc(item)}" for item in digest["decisions"]]

    if digest.get("unanswered"):
        lines += ["", "<b>❓ Ждут ответа</b>"]
        lines += [f"• {esc(item)}" for item in digest["unanswered"]]

    if _filled(digest.get("noise")):
        lines += ["", f"<i>💬 Остальное: {esc(digest['noise'])}</i>"]

    lines += ["", f"<i>{message_count} сообщений обработано</i>"]
    return "\n".join(lines)


def is_empty(digest: dict) -> bool:
    return not any(digest.get(key) for key in ("actions", "money", "events", "decisions", "unanswered"))
