"""
Сводка: разбор ответа модели и рендер в сообщение Telegram.

Сами промпты живут в `prompts.py` — их правят отдельно от логики.
Структуру просим словами, а не через json_schema: её принимают не все
провайдеры, а разбор всё равно нужен свой.
"""

import html
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import llm
from .config import config
from .prompts import JSON_SHAPE, MERGE_SYSTEM, QUESTION_SYSTEM, SYSTEM

EMPTY = {"—", "-", "", "нет", "не указано"}


def _filled(value: object) -> bool:
    """
    Есть ли в значении содержание.

    Отсутствующий ключ — тоже пустота. Без этой проверки `str(None)` даёт
    «none», раздел считается заполненным, и сводка либо печатает человеку
    слово «None», либо падает на `digest["noise"]`, которого модель не прислала.
    """
    return value is not None and str(value).strip().lower() not in EMPTY


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def period_label(hours: int) -> str:
    if hours <= 24:
        return "последние сутки"
    return f"последние {round(hours / 24)} дн."


def window_label(hours: int) -> str:
    """
    Границы окна словами.

    «Сутки» — это последние 24 часа, а не сегодняшний день: вечерняя сводка
    захватывает вчерашний вечер. Пока об этом не сказано, человек сверяет
    счётчик с тем, что видит в чате за сегодня, и числа не сходятся.
    """
    zone = ZoneInfo(config.timezone)
    until = datetime.now(zone)
    since = until - timedelta(hours=hours)
    if since.date() == until.date():
        return f"за {since:%H:%M}–{until:%H:%M}"
    return f"с {since:%H:%M} {since:%d.%m} по {until:%H:%M} {until:%d.%m}"


def memory_note(rows: list[dict]) -> str:
    """
    Пометка для блока «Завтра», собранного не из свежей переписки.

    Без неё пять пунктов, записанных на прошлой неделе, читаются как «бот
    прочитал не сутки, а неделю» — и сводка выглядит перегруженной.
    """
    seen = [parse_date((row.get("first_seen") or "")[:10]) for row in rows]
    seen = [day for day in seen if day]
    if not seen:
        return "из более ранней переписки"
    return f"из более ранней переписки — писали {min(seen):%d.%m}"


def _points(items: list[dict]) -> list[str]:
    """Пункты со временем там, где оно известно."""
    lines = []
    for item in items:
        when = esc(item.get("when")) if _filled(item.get("when")) else ""
        lines.append(f"• {when} — {esc(item.get('what'))}" if when else f"• {esc(item.get('what'))}")
    return lines


def transcript(messages: list[dict], tz: str) -> str:
    zone = ZoneInfo(tz)
    lines = []
    for message in messages:
        stamp = datetime.fromtimestamp(message["time"], zone).strftime("%d.%m %H:%M")
        lines.append(f"[{stamp}] {message['author']}: {message['text']}")
    return "\n".join(lines)


async def build(messages: list[dict], hours: int, provider: str | None = None) -> dict:
    zone = ZoneInfo(config.timezone)
    now = datetime.now(zone)
    today = now.strftime("%A, %d %B %Y")
    tomorrow = (now + timedelta(days=1)).strftime("%A, %d %B %Y")

    prompt = f"""Сегодня {today} ({now:%Y-%m-%d}). Завтра {tomorrow} ({now + timedelta(days=1):%Y-%m-%d}).

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
            stamp = human_date(item.get("date")) or esc(item.get("when", ""))
            if _filled(item.get("time")):
                stamp = f"{stamp} {esc(item['time'])}" if stamp else esc(item["time"])
            lines.append(f"• {stamp} — {esc(item.get('what'))}" if stamp else f"• {esc(item.get('what'))}")

    if digest.get("tomorrow"):
        lines += ["", "<b>🌅 Завтра</b>"]
        if digest.get("tomorrow_note"):
            lines.append(f"<i>{esc(digest['tomorrow_note'])}</i>")
        lines += _points(digest["tomorrow"])

    if digest.get("decisions"):
        lines += ["", "<b>✅ Решения</b>"]
        lines += [f"• {esc(item)}" for item in digest["decisions"]]

    if digest.get("unanswered"):
        lines += ["", "<b>❓ Ждут ответа</b>"]
        lines += [f"• {esc(item)}" for item in digest["unanswered"]]

    if _filled(digest.get("noise")):
        lines += ["", f"<i>💬 Остальное: {esc(digest['noise'])}</i>"]

    lines += ["", f"<i>{message_count} сообщений {window_label(hours)}</i>"]
    return "\n".join(lines)


def render_quiet(digest: dict, hours: int, message_count: int, chat_title: str | None = None) -> str:
    """
    Тихий день: за сутки не случилось ничего, что требует действий.

    Полная сводка с пустыми разделами в такой день читается как перегруз —
    особенно на выходных, где весь её объём даёт блок «Завтра» из памяти.
    Поэтому короткая форма: строка о тишине, трёп одной строкой и завтрашний день.
    """
    title = chat_title or "Родительский чат"
    lines = [
        f"<b>📋 {esc(title)}</b>",
        f"Ничего, что требует действий — {message_count} сообщений {window_label(hours)}.",
    ]

    if _filled(digest.get("noise")):
        lines += ["", f"<i>💬 Остальное: {esc(digest['noise'])}</i>"]

    if digest.get("tomorrow"):
        lines += ["", "<b>🌅 Завтра</b>"]
        if digest.get("tomorrow_note"):
            lines.append(f"<i>{esc(digest['tomorrow_note'])}</i>")
        lines += _points(digest["tomorrow"])

    return "\n".join(lines)


def is_empty(digest: dict) -> bool:
    return not any(
        digest.get(key) for key in ("actions", "money", "events", "tomorrow", "decisions", "unanswered")
    )


# Модель отвечает этим словом, когда в переписке ответа нет, — см. QUESTION_SYSTEM
NOTHING_FOUND = "НЕТ"


def is_nothing(reply: str) -> bool:
    return reply.strip().strip(".!").upper() == NOTHING_FOUND


def tomorrow_items(digest: dict) -> list[dict]:
    """Пункты на завтра в пригодном для хранения виде: только время и суть."""
    items = []
    for item in digest.get("tomorrow") or []:
        what = str(item.get("what", "")).strip()
        if not what:
            continue
        when = str(item.get("when", "")).strip()
        items.append({"when": when if _filled(when) else "", "what": what})
    return items


def render_agenda(blocks: list[tuple[str, list[dict]]], single_chat: bool) -> str:
    """Утреннее напоминание: сегодняшние пункты, собранные вчера вечером."""
    lines = ["<b>🌅 Сегодня</b>"]
    for title, items in blocks:
        if not single_chat:
            lines.append(f"\n<b>{esc(title)}</b>")
        lines += _points(items)
    return "\n".join(lines)


DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def human_date(value: object) -> str:
    """2026-09-07 → «07.09 (пн)». Год дописываем, когда он не нынешний."""
    parsed = parse_date(value)
    if not parsed:
        return ""
    today = datetime.now(ZoneInfo(config.timezone)).date()
    stamp = f"{parsed:%d.%m}" if parsed.year == today.year else f"{parsed:%d.%m.%Y}"
    return f"{stamp} ({DAYS[parsed.weekday()]})"


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError):
        return None


# Заглушки вроде «… (ДЗ есть, продолжение обрезано)» модель иногда выдаёт вместо
# признания, что содержания не знает. В календаре им делать нечего.
STUB = re.compile(r"^[…\.\s]*$|обрезан|неизвестн|уточн[ия]ть у|не указан")


def dated_events(digest: dict) -> list[dict]:
    """События с разобранной датой — только они годятся для календаря."""
    today = datetime.now(ZoneInfo(config.timezone)).date()
    items = []
    for item in digest.get("events") or []:
        parsed = parse_date(item.get("date"))
        what = str(item.get("what", "")).strip()
        if not parsed or not what or len(what) < 6 or STUB.search(what.lower()):
            continue

        # Модель путает учебный год с календарным: «срез в апреле», сказанное
        # в сентябре, приезжает апрелем этого года, то есть в прошлое
        if (today - parsed).days > 30:
            try:
                parsed = parsed.replace(year=parsed.year + 1)
            except ValueError:  # 29 февраля
                continue
        moment = str(item.get("time", "")).strip()
        items.append(
            {
                "date": parsed.isoformat(),
                "time": moment if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", moment) else "",
                "what": what,
            }
        )
    return items


async def merge(items: list[dict], provider: str | None = None) -> list[dict]:
    """
    Склеивает дубли в списке на один день.

    Нужна потому, что событие живёт в переписке неделю: линейку 1 сентября
    в живом чате записали четыре раза разными словами за четыре дня. Без
    склейки человек получит четыре строки об одном и том же.
    """
    if len(items) < 2:
        return items

    listing = "\n".join(f"- {item.get('when') or 'без времени'}: {item.get('what')}" for item in items)
    try:
        raw = await llm.complete(MERGE_SYSTEM, listing, json_mode=True, provider=provider)
        merged = llm.extract_json(raw).get("items")
    except Exception:  # noqa: BLE001 — склейка не стоит того, чтобы терять сводку
        merged = None

    if not merged:
        # Запасной вариант: гасим хотя бы дословные совпадения
        seen, plain = set(), []
        for item in items:
            key = " ".join(str(item.get("what", "")).lower().split())[:80]
            if key not in seen:
                seen.add(key)
                plain.append(item)
        return plain

    return [
        {"when": str(item.get("when", "")).strip(), "what": str(item.get("what", "")).strip()}
        for item in merged
        if str(item.get("what", "")).strip()
    ]


def render_ahead(rows: list[dict], single_chat: bool) -> str:
    """
    Что записано на ближайшие дни.

    Рядом с давними записями ставим, когда о них писали: если про событие
    сказали неделю назад и с тех пор молчат, это стоит знать.
    """
    lines = ["<b>🔭 Что впереди</b>"]
    current = None
    today = datetime.now(ZoneInfo(config.timezone)).date()

    for row in rows:
        if row["date"] != current:
            current = row["date"]
            parsed = parse_date(current)
            mark = human_date(current)
            if parsed == today:
                mark += " — сегодня"
            elif parsed and (parsed - today).days == 1:
                mark += " — завтра"
            lines += ["", f"<b>{mark}</b>"]

        head = f"{esc(row['when'])} — " if row["when"] else ""
        tail = ""
        seen = parse_date((row.get("first_seen") or "")[:10])
        if seen and (today - seen).days >= 3:
            tail = f" <i>(писали {seen:%d.%m})</i>"
        chat = f" <i>· {esc(row['title'])}</i>" if not single_chat else ""
        lines.append(f"• {head}{esc(row['what'])}{chat}{tail}")

    return "\n".join(lines)
