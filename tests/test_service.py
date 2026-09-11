"""
Сборка и доставка: путь от истории MAX до сообщения в Telegram.

Тут проверяется сцепка целиком — чтение окна, модель, календарь, рендер и
отправка, — потому что ломается обычно не функция, а стык между ними.
Наружу выходят только два двойника: MAX и модель.
"""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, digest, service
from app.bot import texts
from tests import fakes
from tests.fakes import FakeMax, message, photo

ZONE = ZoneInfo("Europe/Moscow")
MERGE = "короткий итоговый план на день"


def tomorrow() -> str:
    return (datetime.now(ZONE) + timedelta(days=1)).strftime("%Y-%m-%d")


def digest_json(**overrides) -> str:
    data = {
        "headline": "Собрание в четверг",
        "actions": [{"task": "Подписать дневник", "deadline": "до 12.09", "details": "—"}],
        "money": [],
        "events": [],
        "tomorrow": [],
        "decisions": [],
        "unanswered": [],
        "noise": "поздравления",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def chatter(count: int = 6) -> list:
    return [message(i, minutes_ago=i * 10, text=f"сообщение {i}") for i in range(count)]


# --- Сводка ---


async def test_сводка_уходит_по_одной_на_каждый_чат(monkeypatch, bot, user, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    sent = await service.send_digest(bot, user, hours=24)

    assert sent == 2, "у пользователя два чата — две сводки"
    assert bot.said("5 «З» класс") and bot.said("Секция плавания")


async def test_под_сводкой_стоят_кнопки_этого_чата(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    await service._digest_chat(bot, user, chat, hours=72, quiet_if_empty=False)

    keyboard = bot.messages[-1][2]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == [f"ask:{chat.chat_id}", f"pics:{chat.chat_id}:72"]


async def test_без_телефона_и_чатов_сводка_не_собирается(bot):
    person = db.create_user(7, "новичок")
    assert await service.send_digest(bot, person) == 0
    assert bot.messages == []


async def test_пустой_чат_по_запросу_отвечает_словами(monkeypatch, bot, user, chat):
    fakes.install(monkeypatch, FakeMax([]))
    assert await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False) is False
    assert "тихо" in bot.last


async def test_пустой_чат_по_расписанию_молчит(monkeypatch, bot, user, chat):
    fakes.install(monkeypatch, FakeMax([]))
    assert await service._digest_chat(bot, user, chat, 24, quiet_if_empty=True) is False
    assert bot.messages == [], "по расписанию человека пустотой не будят"


async def test_тихий_день_без_планов_проходит_совсем_молча(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter(3)))
    fake_llm.returns(digest_json(actions=[], headline="болтали"))

    assert await service._digest_chat(bot, user, chat, 24, quiet_if_empty=True) is False
    assert bot.messages == []
    assert db.recent_events()[0]["kind"] == "digest_skipped"


async def test_тихий_день_с_планами_на_завтра_даёт_короткую_форму(monkeypatch, bot, user, chat, fake_llm):
    """В свежей переписке пусто, но календарь помнит завтрашнее — молчать нельзя."""
    db.save_calendar(user.telegram_id, chat.chat_id, [{"date": tomorrow(), "what": "физкультура, форма"}])
    fakes.install(monkeypatch, FakeMax(chatter(3)))
    fake_llm.returns(digest_json(actions=[], headline="болтали"))

    assert await service._digest_chat(bot, user, chat, 24, quiet_if_empty=True) is True
    assert "Ничего, что требует действий" in bot.last
    assert "физкультура, форма" in bot.last
    assert db.recent_events()[0]["kind"] == "digest_quiet"


async def test_сводка_отмечает_время_и_гасит_счётчик_сбоев(monkeypatch, bot, user, fake_llm):
    db.note_failure(user.telegram_id, "вчера не смог")
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    await service.send_digest(bot, user, hours=24)

    assert db.get_user(user.telegram_id).failures == 0
    with db.connect() as conn:
        row = conn.execute("SELECT last_digest_at FROM users WHERE telegram_id = 500").fetchone()
    assert row["last_digest_at"] is not None


async def test_расход_на_модель_записывается(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    report = db.usage_report()
    assert report and report[0]["calls"] >= 1


# --- Календарь ---


async def test_события_из_сводки_попадают_в_календарь(monkeypatch, bot, user, chat, fake_llm):
    when = (datetime.now(ZONE).date() + timedelta(days=4)).isoformat()
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json(events=[{"date": when, "time": "18:30", "what": "Родительское собрание"}]))

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    rows = db.calendar_on(user.telegram_id, when)
    assert [r["what"] for r in rows] == ["Родительское собрание"]
    assert rows[0]["when"] == "18:30"


async def test_блок_завтра_пополняется_из_памяти(monkeypatch, bot, user, chat, fake_llm):
    """Про поднятие флага сказали неделю назад — суточная сводка это не увидит."""
    db.save_calendar(user.telegram_id, chat.chat_id, [{"date": tomorrow(), "what": "поднятие флага, 8:20"}])
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    assert "поднятие флага" in bot.last
    assert "из более ранней переписки" in bot.last, "иначе читается как «бот прочитал неделю»"


async def test_свежий_план_на_завтра_идёт_без_пометки_о_памяти(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json(tomorrow=[{"when": "09:00", "what": "физкультура"}]))

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    assert "физкультура" in bot.last
    assert "из более ранней переписки" not in bot.last


async def test_дубли_в_календаре_склеиваются_моделью(monkeypatch, bot, user, chat, fake_llm):
    db.save_calendar(user.telegram_id, chat.chat_id, [{"date": tomorrow(), "what": "линейка в 9 утра"}])
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json(tomorrow=[{"when": "09:00", "what": "линейка, форма парадная"}]))
    fake_llm.when(MERGE, json.dumps({"items": [{"when": "09:00", "what": "линейка, форма парадная"}]},
                                    ensure_ascii=False))

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    assert [r["what"] for r in db.calendar_on(user.telegram_id, tomorrow())] == ["линейка, форма парадная"]


# --- Утреннее напоминание ---


async def test_утро_молчит_когда_на_сегодня_ничего_нет(bot, user):
    assert await service.send_morning(bot, user) is False
    assert bot.messages == []
    assert db.get_user(user.telegram_id).last_morning is not None, "отметка нужна и в пустой день"


async def test_утро_собирается_из_календаря(bot, user, chat, fake_llm):
    today = datetime.now(ZONE).strftime("%Y-%m-%d")
    db.save_calendar(user.telegram_id, chat.chat_id, [{"date": today, "time": "09:00", "what": "линейка"}])

    assert await service.send_morning(bot, user) is True
    assert "🌅 Сегодня" in bot.last and "линейка" in bot.last


async def test_утро_с_двумя_чатами_подписывает_блоки(bot, user, fake_llm):
    today = datetime.now(ZONE).strftime("%Y-%m-%d")
    for chat in user.chats:
        db.save_calendar(user.telegram_id, chat.chat_id, [{"date": today, "what": f"дело из {chat.title}"}])

    await service.send_morning(bot, user)

    assert "5 «З» класс" in bot.last and "Секция плавания" in bot.last


# --- Вопросы ---


async def test_вопрос_по_одному_чату_не_веерит(monkeypatch, bot, user, chat, fake_llm):
    client = fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("Собрание в четверг в 18:30 (из чата 28.08)")

    await service.answer_question(bot, user, "когда собрание?", chat=chat)

    assert client.chats_asked == [chat.chat_id], "спрошен ровно один чат"
    assert fake_llm.asked == 1, "и модель дёрнута один раз"
    assert "Секция плавания" not in bot.last, "название чата в шапке лишнее, чат и так один"


async def test_вопрос_без_чата_идёт_по_всем(monkeypatch, bot, user, fake_llm):
    client = fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("Собрание в четверг (из чата 28.08)")

    await service.answer_question(bot, user, "когда собрание?")

    assert client.chats_asked == [-100, -200]
    assert "5 «З» класс" in bot.last and "Секция плавания" in bot.last


async def test_чаты_без_ответа_молчат(monkeypatch, bot, user, fake_llm):
    """Три вежливых «не нашёл» вместо одного ответа — худшее, что можно сделать."""
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("Собрание в четверг (из чата 28.08)", "НЕТ")

    await service.answer_question(bot, user, "когда собрание?")

    assert "Секция плавания" not in bot.last


async def test_ответа_нет_нигде(monkeypatch, bot, user, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("НЕТ")

    await service.answer_question(bot, user, "когда собрание?")

    assert bot.last == texts.ANSWER_EMPTY


async def test_ответа_нет_в_названном_чате(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("НЕТ")

    await service.answer_question(bot, user, "когда собрание?", chat=chat)

    assert "5 «З» класс" in bot.last, "искали в одном чате — про него и отвечаем"


async def test_вопрос_записывается_в_журнал_с_чатом(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns("НЕТ")

    await service.answer_question(bot, user, "когда собрание?", chat=chat)

    event = db.recent_events(telegram_id=user.telegram_id)[0]
    assert event["kind"] == "question_asked"
    assert event["detail"].startswith("5 «З» класс: ")


# --- Фотографии ---


def photos_in(monkeypatch, *ids, minutes_ago: float = 5):
    return fakes.install(monkeypatch, FakeMax([
        message(i, minutes_ago=minutes_ago + i, text="", attaches=[photo(pid)])
        for i, pid in enumerate(ids)
    ]))


async def test_фотографии_пересылаются_с_подписью(monkeypatch, bot, user, chat):
    photos_in(monkeypatch, 7, 8)
    monkeypatch.setattr(service.max_client, "download_photo", _gives(b"jpeg"))

    assert await service.send_photos(bot, user, chat, hours=24) == 2
    assert "🖼 Фото из чата" in bot.messages[0][1]
    assert len(bot.photos) == 2
    assert "Человек10 · " in bot.photos[0][2]


async def test_подпись_из_чата_едет_под_фотографией(monkeypatch, bot, user, chat):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=5, text="такую тетрадь?", attaches=[photo(7)])
    ]))
    monkeypatch.setattr(service.max_client, "download_photo", _gives(b"jpeg"))

    await service.send_photos(bot, user, chat, hours=24)

    assert "такую тетрадь?" in bot.photos[0][2]


async def test_фотографий_нет_говорим_словами(monkeypatch, bot, user, chat):
    fakes.install(monkeypatch, FakeMax(chatter()))

    assert await service.send_photos(bot, user, chat, hours=72) == 0
    assert "последние 3 дн." in bot.last
    assert bot.photos == []


async def test_протухшие_ссылки_объясняются_человеку(monkeypatch, bot, user, chat):
    photos_in(monkeypatch, 7, 8)
    monkeypatch.setattr(service.max_client, "download_photo", _gives(None))

    assert await service.send_photos(bot, user, chat, hours=24) == 0
    assert bot.last == texts.PHOTOS_FAILED


async def test_одна_битая_картинка_не_отменяет_остальные(monkeypatch, bot, user, chat):
    photos_in(monkeypatch, 7, 8, 9)

    async def flaky(url):
        return None if url.startswith("https://max.test/8") else b"jpeg"

    monkeypatch.setattr(service.max_client, "download_photo", flaky)

    assert await service.send_photos(bot, user, chat, hours=24) == 2


async def test_больше_десятка_фотографий_не_шлём(monkeypatch, bot, user, chat):
    photos_in(monkeypatch, *range(100, 120))
    monkeypatch.setattr(service.max_client, "download_photo", _gives(b"jpeg"))

    sent = await service.send_photos(bot, user, chat, hours=24)
    assert sent == service.PHOTOS_SHOWN


def _gives(value):
    async def download(url):
        return value

    return download


# --- Сбои ---


async def test_сбой_чтения_объясняется_человеку(monkeypatch, bot, user, chat):
    monkeypatch.setattr(service.max_client, "fetch_window", _raises(RuntimeError("not authorized")))

    assert await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False) is False
    assert bot.said("Сессия MAX отвалилась")
    assert db.get_user(user.telegram_id).failures == 1


async def test_сбой_модели_не_советует_перезаходить_в_max(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    monkeypatch.setattr(digest, "build", _raises(RuntimeError("401 invalid token")))

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=False)

    assert bot.said("Модель не ответила")
    assert not bot.said("/start")


async def test_по_расписанию_о_повторном_сбое_молчим(monkeypatch, bot, user, chat):
    """Ежедневное «опять не смог» превращает сервис в источник раздражения."""
    monkeypatch.setattr(service.max_client, "fetch_window", _raises(RuntimeError("timeout")))

    await service._digest_chat(bot, user, chat, 24, quiet_if_empty=True)
    first = len(bot.messages)
    await service._digest_chat(bot, user, db.get_user(user.telegram_id).chats[0], 24, quiet_if_empty=True)

    assert first == 1, "в первый раз сказать надо"
    assert len(bot.messages) == 1, "во второй — уже нет"


async def test_затянувшийся_сбой_зовёт_админа(bot, user):
    for _ in range(service.ALARM_AT - 1):
        await service.report_failure(bot, user, RuntimeError("timeout"), announce=False)
    admin_before = [m for m in bot.messages if m[0] == 900]

    await service.report_failure(bot, user, RuntimeError("timeout"), announce=False)

    admin_after = [m for m in bot.messages if m[0] == 900]
    assert not admin_before and admin_after, f"админа зовут на {service.ALARM_AT}-м сбое подряд"


async def test_смертельный_сбой_зовёт_админа_сразу(bot, user):
    await service.report_failure(bot, user, RuntimeError("not authorized"), announce=False)
    assert any(m[0] == 900 for m in bot.messages)


async def test_заблокировавший_бота_пользователь_не_роняет_рассылку(bot, user):
    bot.refuse = RuntimeError("bot was blocked by the user")
    failure = await service.report_failure(bot, user, RuntimeError("timeout"))
    assert failure.kind == "network"


def _raises(exc):
    async def boom(*args, **kwargs):
        raise exc

    return boom


# --- Длинные сообщения ---


async def test_короткое_сообщение_уходит_целиком(bot):
    await service.send_long(bot, 1, "коротко")
    assert bot.texts == ["коротко"]


async def test_длинное_режется_по_строкам(bot):
    text = "\n".join(f"строка {i} " + "х" * 100 for i in range(60))
    await service.send_long(bot, 1, text)

    assert len(bot.messages) > 1
    assert all(len(t) <= service.LIMIT for t in bot.texts)
    assert "".join(t.replace("\n", "") for t in bot.texts).count("строка") == 60


async def test_разрез_предпочитает_границу_раздела(bot):
    body = "\n".join("х" * 90 for _ in range(40))
    text = f"<b>Первый</b>\n{body}\n<b>Второй</b>\n{body}"
    await service.send_long(bot, 1, text)

    assert len(bot.messages) == 2
    assert bot.texts[1].startswith("<b>Второй</b>")


async def test_клавиатура_достаётся_последнему_куску(bot):
    from app.bot import keyboards

    text = "\n".join(f"строка {i} " + "х" * 100 for i in range(60))
    keyboard = keyboards.under_digest(-100, 24)
    await service.send_long(bot, 1, text, keyboard)

    marks = [m[2] is not None for m in bot.messages]
    assert marks == [False] * (len(marks) - 1) + [True]


# --- Внеочередная сводка ---


async def test_внеочередная_сводка_объясняет_почему_пришла(monkeypatch, bot, user, chat, fake_llm):
    fakes.install(monkeypatch, FakeMax(chatter()))
    fake_llm.returns(digest_json())

    await service.send_burst(bot, user, chat, hours=3)

    assert "необычно много сообщений" in bot.texts[0]
    assert db.get_user(user.telegram_id).last_burst_at is not None
