"""
Сводка: промпт, разбор ответа модели и рендер в сообщение Telegram.

Структуру просим словами, а не через json_schema: её принимают не все
провайдеры, а разбор всё равно нужен свой.
"""

import html
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import llm
from .config import config

SYSTEM = """Ты — ассистент занятого родителя. Он не читает родительский чат школы вообще и полагается только на твою сводку.

Твоя задача — вытащить из болтовни то, что имеет практические последствия, и безжалостно выкинуть остальное.

Правила:
- Пиши по-русски, коротко, без воды и без вежливых вступлений.
- Никогда не выдумывай факты, суммы и даты. Если чего-то нет в переписке — ставь «—».
- Относительные даты («в пятницу», «завтра») переводи в конкретные числа, опираясь на текущую дату.
- В events поле date — строго вида ГГГГ-ММ-ДД. Если известен только месяц, поставь первое число этого месяца. Если даже месяц не назван, событию в events не место.
- Год определяй по учебному году, а не по календарному: он идёт с сентября по июнь. Если названный месяц в текущем году уже прошёл, речь о следующем годе. В сентябре 2026 «срез в апреле» — это апрель 2027, а не апрель 2026.
- Не выдумывай событий-заглушек. Если из переписки понятно, что задание есть, но какое — неизвестно, событию в events не место.
- В events поле time — только если время названо в переписке, вида ЧЧ:ММ. «После уроков» и «во второй половине дня» — это не время, ставь «—».
- Если несколько сообщений про одно и то же — объединяй в один пункт.
- Отменённое или изменённое позже по ходу переписки указывай в итоговом, актуальном виде.
- Поздравления, спасибо, стикеры, споры ни о чём — это шум, ему место только в поле noise.
- Чужие телефоны, адреса и прочие личные контакты в сводку не переноси: обмен ими — это шум. Единственное исключение — реквизиты для оплаты: если деньги переводят по номеру телефона или карты, номер нужен, без него поручение невыполнимо.
- Пустой раздел — это нормально, оставь пустой список, не придумывай наполнение.
- Раздел tomorrow — только то, что происходит именно завтра (дату завтрашнего дня тебе дают ниже).
  Это дублирование: то же событие может стоять и в events, и в tomorrow. Так и надо —
  утром человек получит короткое напоминание именно об этих пунктах и больше ни о чём.

Вложения. Вместо них в переписке стоят пометки в квадратных скобках:
[фото], [файл «имя»], [опрос «вопрос»; варианты: ...], а иногда [на фото: описание].
Что с ними делать:
- Опрос — это почти всегда решение или его обсуждение. Разбирай его как обычное содержание: вопрос и варианты тебе видны.
- Пометка [на фото: ...] означает, что картинку уже посмотрели за тебя. Считай её описание обычным содержанием сообщения и вытаскивай оттуда даты, суммы и задания.
- Если пометка [на фото: ...] говорит про список класса с контактами — это шум, персональные данные в сводку не тащим.
- Если описания нет, а по переписке понятно, что на фото или в файле лежит нужное, напиши об этом в разделе с действиями: «прислали расписание картинкой — посмотреть в чате за такое-то число». Не притворяйся, будто знаешь содержимое.
- Просто фотографии с праздника и прочие снимки без последствий — это шум."""

JSON_SHAPE = """Ответь строго одним JSON-объектом без пояснений вокруг:
{
  "headline": "одна строка — главное за период",
  "actions": [{"task": "что сделать", "deadline": "до 12.09 или —", "details": "уточнение или —"}],
  "money":   [{"what": "за что", "amount": "сумма или —", "deadline": "срок или —", "recipient": "кому или —"}],
  "events":  [{"date": "2026-09-07", "time": "09:00 или —", "what": "что происходит"}],
  "tomorrow": [{"when": "время вроде 09:00 или —", "what": "что происходит завтра"}],
  "decisions": ["до чего договорились"],
  "unanswered": ["открытые вопросы, где ждут ответа родителей"],
  "noise": "одной строкой: о чём был остальной трёп"
}
Все восемь ключей обязательны. Пустой список — это [], пустая строка — "—"."""

QUESTION_SYSTEM = """Ты отвечаешь на вопросы родителя по переписке школьного родительского чата.

Как отвечать:
- Сразу давай готовый ответ своими словами, одним-тремя предложениями. Это главное.
- В конце добавь в скобках дату сообщения-источника, например «(из чата 28.08)».
- Никогда не пересказывай переписку списком и не приводи цитаты целиком — родитель просит ответ, а не выписку.
- Если в чате есть только косвенно связанное — скажи, что прямого ответа нет, и коротко приведи то, что есть.
- Если в переписке про это совсем ничего нет — ответь одним словом: НЕТ. Без пояснений и извинений.
  Так я пойму, что этот чат можно не показывать, и не буду тревожить человека пустым ответом.
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
        for item in digest["tomorrow"]:
            when = esc(item.get("when")) if _filled(item.get("when")) else ""
            lines.append(f"• {when} — {esc(item.get('what'))}" if when else f"• {esc(item.get('what'))}")

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
        for item in items:
            when = esc(item.get("when")) if _filled(item.get("when")) else ""
            lines.append(f"• {when} — {esc(item.get('what'))}" if when else f"• {esc(item.get('what'))}")
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


MERGE_SYSTEM = """Тебе дают всё, что чат насобирал про один день: пункты из сообщений за разные дни. Про одно событие в чате пишут по нескольку раз — уточняют время, добавляют подробности, иногда меняют решение на противоположное.

Составь из этого короткий итоговый план на день.

- Одно событие — один пункт. Если несколько строк про одно и то же, собери их детали в один пункт, а не выбирай одну из формулировок.
- Приход в школу, начало мероприятия и его окончание в один день — это одно событие, а не три. Возьми время начала.
- Если строки противоречат друг другу, верна поздняя: детали в чате уточняют, а не отменяют задним числом.
- Время ставь самое точное из известных. Если ни в одной строке времени нет — оставь пустым.
- Пунктов должно быть не больше шести. Если получается больше, значит ты недостаточно склеил.
- Каждый пункт — одна короткая строка, до ста двадцати знаков. Это утреннее напоминание, его читают на бегу.
- Оставляй то, что меняет действия родителя: время, место, что взять с собой, кого касается. Подробности организации, от которых родителю ничего не нужно делать, выбрасывай.
- Того, чего нет во входных строках, не добавляй.

Ответь строго одним JSON-объектом: {"items": [{"when": "09:00 или пустая строка", "what": "что происходит"}]}"""


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
