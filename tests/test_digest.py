"""
Сводка: разбор ответа модели и превращение его в сообщение.

Модель здесь заглушена — проверяется наш код вокруг неё. Больше всего
внимания датам: на них уже ловились и прошлое вместо будущего, и заглушки
вида «(ДЗ есть, продолжение обрезано)», уехавшие в календарь.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import digest

ZONE = ZoneInfo("Europe/Moscow")


def days_from_now(shift: int) -> str:
    return (datetime.now(ZONE).date() + timedelta(days=shift)).isoformat()


FULL = {
    "headline": "Собрание в четверг, сдать 500 ₽",
    "actions": [{"task": "Подписать дневник", "deadline": "до 12.09", "details": "—"}],
    "money": [{"what": "Экскурсия", "amount": "500 ₽", "deadline": "—", "recipient": "Ирине"}],
    "events": [{"date": days_from_now(3), "time": "18:30", "what": "Родительское собрание"}],
    "tomorrow": [{"when": "09:00", "what": "Физкультура, форма"}],
    "decisions": ["Едем на автобусе"],
    "unanswered": ["Кто сопровождает?"],
    "noise": "поздравления с днём знаний",
}


# --- Подписи периода ---


@pytest.mark.parametrize(
    "hours, expected",
    [(1, "последние сутки"), (24, "последние сутки"), (72, "последние 3 дн."), (168, "последние 7 дн.")],
)
def test_подпись_периода(hours, expected):
    assert digest.period_label(hours) == expected


def test_окно_внутри_суток_показывает_только_часы():
    label = digest.window_label(1)
    assert label.startswith("за ") and "–" in label


def test_окно_через_полночь_называет_даты():
    assert digest.window_label(48).startswith("с ")


# --- Рендер ---


def test_сводка_содержит_все_заполненные_разделы():
    text = digest.render(FULL, 24, 42, "5 «З»")
    for mark in ("🔴 Требуется от меня", "💰 Деньги", "📅 Даты и события",
                 "🌅 Завтра", "✅ Решения", "❓ Ждут ответа", "💬 Остальное"):
        assert mark in text, f"пропал раздел {mark}"
    assert "42 сообщений" in text
    assert "5 «З»" in text


def test_пустые_разделы_не_рисуются():
    text = digest.render({"headline": "Тихо", "noise": "—"}, 24, 3, "Чат")
    assert "🔴" not in text and "💰" not in text
    assert "💬 Остальное" not in text, "прочерк в noise — это пустота, а не содержание"


def test_сводка_без_необязательных_ключей_не_падает():
    """
    Модель не обязана прислать все восемь ключей, а сводка из-за этого падала:
    отсутствующий ключ считался заполненным, и `digest["noise"]` бросал KeyError.
    """
    text = digest.render({"headline": "Одни разговоры"}, 24, 7, "Чат")
    assert "Одни разговоры" in text
    assert "None" not in text


def test_отсутствующее_поле_пункта_не_печатается_словом_none():
    text = digest.render({"actions": [{"task": "Подписать дневник"}]}, 24, 1)
    assert "None" not in text


def test_прочерки_внутри_пункта_не_печатаются():
    text = digest.render({"actions": [{"task": "Подписать дневник", "deadline": "—", "details": "—"}]}, 24, 1)
    assert "Подписать дневник" in text
    assert "—" not in text.split("Подписать дневник")[1].split("\n")[0]


def test_угловые_скобки_в_названии_чата_экранируются():
    """Иначе Telegram не доставит сообщение целиком, и человек увидит тишину."""
    text = digest.render(FULL, 24, 1, "5 <З> & друзья")
    assert "&lt;З&gt;" in text and "&amp;" in text


def test_тихая_сводка_короткая_но_с_завтрашним_днём():
    text = digest.render_quiet(FULL, 24, 4, "Чат")
    assert "Ничего, что требует действий" in text
    assert "🌅 Завтра" in text
    assert "🔴" not in text, "в тихой форме разделу действий не место"


def test_пометка_о_происхождении_блока_завтра():
    data = dict(FULL, tomorrow_note="из более ранней переписки — писали 28.08")
    assert "<i>из более ранней переписки — писали 28.08</i>" in digest.render(data, 24, 1)


def test_утреннее_напоминание_по_одному_чату_без_заголовков():
    blocks = [("5 «З»", [{"when": "09:00", "what": "линейка"}])]
    assert "5 «З»" not in digest.render_agenda(blocks, single_chat=True)
    assert "5 «З»" in digest.render_agenda(blocks, single_chat=False)


def test_что_впереди_помечает_сегодня_и_завтра():
    rows = [
        {"date": days_from_now(0), "when": "", "what": "сегодняшнее", "title": "Чат", "first_seen": None},
        {"date": days_from_now(1), "when": "09:00", "what": "завтрашнее", "title": "Чат", "first_seen": None},
    ]
    text = digest.render_ahead(rows, single_chat=True)
    assert "— сегодня" in text and "— завтра" in text


def test_что_впереди_отмечает_давние_записи():
    long_ago = (date.today() - timedelta(days=9)).isoformat()
    rows = [{"date": days_from_now(2), "when": "", "what": "флаг", "title": "Чат", "first_seen": long_ago}]
    assert "(писали" in digest.render_ahead(rows, single_chat=True)

    fresh = [{"date": days_from_now(2), "when": "", "what": "флаг", "title": "Чат",
              "first_seen": date.today().isoformat()}]
    assert "(писали" not in digest.render_ahead(fresh, single_chat=True)


# --- Пустота и «не нашёл» ---


def test_пустой_считается_сводка_без_единого_дела():
    assert digest.is_empty({"headline": "всё тихо", "noise": "болтали"})
    assert not digest.is_empty({"actions": [{"task": "что-то"}]})
    assert not digest.is_empty({"tomorrow": [{"what": "физкультура"}]})


@pytest.mark.parametrize("reply", ["НЕТ", "нет", "НЕТ.", "  НЕТ!  "])
def test_ответ_нет_распознаётся(reply):
    assert digest.is_nothing(reply)


def test_ответ_начинающийся_с_нет_не_считается_пустым():
    assert not digest.is_nothing("Нет, собрание перенесли на 14-е (из чата 28.08)")


# --- Даты ---


def test_разбор_даты():
    assert digest.parse_date("2026-09-07") == date(2026, 9, 7)
    assert digest.parse_date("2026-09-07T10:00") == date(2026, 9, 7)
    assert digest.parse_date("в пятницу") is None
    assert digest.parse_date(None) is None


def test_человеческая_дата_с_днём_недели():
    assert digest.human_date("2026-09-07") == "07.09 (пн)"


def test_чужой_год_дописывается():
    other = date.today().replace(year=date.today().year + 1)
    assert str(other.year) in digest.human_date(other.isoformat())


def test_события_без_разобранной_даты_в_календарь_не_едут():
    events = {"events": [
        {"date": "в пятницу", "what": "собрание родителей"},
        {"date": days_from_now(2), "what": "собрание родителей"},
    ]}
    assert [e["what"] for e in digest.dated_events(events)] == ["собрание родителей"]


@pytest.mark.parametrize("what", ["…", "ДЗ есть, продолжение обрезано", "уточнять у классного", "неизвестно"])
def test_заглушки_модели_в_календарь_не_едут(what):
    assert digest.dated_events({"events": [{"date": days_from_now(2), "what": what}]}) == []


def test_слишком_короткое_событие_отбрасывается():
    assert digest.dated_events({"events": [{"date": days_from_now(2), "what": "ДЗ"}]}) == []


def test_прошедшая_дата_переносится_на_учебный_год_вперёд():
    """«Срез в апреле», сказанное в сентябре, модель отдаёт апрелем этого года."""
    past = (datetime.now(ZONE).date() - timedelta(days=90)).isoformat()
    got = digest.dated_events({"events": [{"date": past, "what": "срез по математике"}]})
    assert digest.parse_date(got[0]["date"]).year == digest.parse_date(past).year + 1


def test_недавнее_прошлое_не_переносится():
    """Вчерашнее событие — это вчерашнее событие, а не через год."""
    recent = (datetime.now(ZONE).date() - timedelta(days=2)).isoformat()
    got = digest.dated_events({"events": [{"date": recent, "what": "контрольная работа"}]})
    assert got[0]["date"] == recent


@pytest.mark.parametrize("moment, expected", [("18:30", "18:30"), ("9:00", "9:00"),
                                              ("после уроков", ""), ("—", ""), ("25:00", "")])
def test_в_календарь_едет_только_настоящее_время(moment, expected):
    got = digest.dated_events({"events": [{"date": days_from_now(2), "time": moment, "what": "собрание класса"}]})
    assert got[0]["time"] == expected


def test_пункты_на_завтра_чистятся_от_прочерков():
    items = digest.tomorrow_items({"tomorrow": [
        {"when": "—", "what": "физкультура"},
        {"when": "09:00", "what": "линейка"},
        {"when": "10:00", "what": "   "},
    ]})
    assert items == [{"when": "", "what": "физкультура"}, {"when": "09:00", "what": "линейка"}]


def test_пометка_памяти_называет_дату_самого_раннего_упоминания():
    rows = [{"first_seen": "2026-08-28 10:00:00"}, {"first_seen": "2026-09-01 10:00:00"}]
    assert digest.memory_note(rows) == "из более ранней переписки — писали 28.08"
    assert digest.memory_note([]) == "из более ранней переписки"


# --- Обращения к модели ---


async def test_сводка_просит_модель_и_разбирает_json(fake_llm):
    fake_llm.returns(f"```json\n{json.dumps(FULL, ensure_ascii=False)}\n```")
    result = await digest.build([{"time": 1757000000, "author": "А", "text": "привет"}], 24)
    assert result["headline"] == FULL["headline"]

    system, prompt = fake_llm.calls[0]
    assert "--- НАЧАЛО ПЕРЕПИСКИ ---" in prompt
    assert "А: привет" in prompt, "в промпт должны уходить и автор, и текст"


async def test_склейка_меньше_двух_пунктов_модель_не_беспокоит(fake_llm):
    items = [{"when": "09:00", "what": "линейка"}]
    assert await digest.merge(items) == items
    assert fake_llm.asked == 0


async def test_склейка_возвращает_то_что_ответила_модель(fake_llm):
    fake_llm.returns(json.dumps({"items": [{"when": "09:00", "what": "линейка, форма парадная"}]},
                                ensure_ascii=False))
    merged = await digest.merge([
        {"when": "", "what": "линейка в 9"},
        {"when": "09:00", "what": "форма парадная на линейку"},
    ])
    assert merged == [{"when": "09:00", "what": "линейка, форма парадная"}]


async def test_молчащая_модель_не_роняет_склейку_а_гасит_дословные_повторы(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(digest.llm, "complete", broken)
    merged = await digest.merge([
        {"when": "", "what": "Линейка"},
        {"when": "", "what": "линейка"},
        {"when": "09:00", "what": "форма парадная"},
    ])
    assert [item["what"] for item in merged] == ["Линейка", "форма парадная"]
