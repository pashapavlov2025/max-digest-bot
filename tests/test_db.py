"""
Хранилище.

Проверяем не SQL, а обещания, на которые опирается остальной код: что
пользователь «готов» только с чатами, что календарь не копит формулировки,
что код приглашения одноразовый и что /stop уносит всё.
"""

from datetime import date, timedelta

from app import db


def today(shift: int = 0) -> str:
    return (date.today() + timedelta(days=shift)).isoformat()


# --- Пользователь ---


def test_новый_пользователь_ещё_не_готов():
    person = db.create_user(1, "petya")
    assert not person.is_ready
    assert person.titles == "—"


def test_готов_только_с_чатами_и_состоянием_ready():
    db.create_user(1, "petya")
    db.update_user(1, state="ready")
    assert not db.get_user(1).is_ready, "состояние без чатов не делает пользователя готовым"

    db.set_chats(1, [(-100, "5 «З»")])
    assert db.get_user(1).is_ready
    assert db.get_user(1).titles == "5 «З»"


def test_повторное_создание_не_затирает_настройки():
    db.create_user(1, "petya")
    db.update_user(1, digest_time="08:15")
    db.create_user(1, "petya")
    assert db.get_user(1).digest_time == "08:15"


def test_set_chats_заменяет_набор_целиком():
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "Первый"), (-200, "Второй")])
    db.set_chats(1, [(-300, "Третий")])

    chats = db.get_user(1).chats
    assert [c.chat_id for c in chats] == [-300]


def test_порядок_чатов_сохраняется():
    db.create_user(1, "petya")
    db.set_chats(1, [(-300, "Третий"), (-100, "Первый"), (-200, "Второй")])
    assert [c.title for c in db.get_user(1).chats] == ["Третий", "Первый", "Второй"]


def test_ready_users_пропускает_паузу_и_бесчатных():
    db.create_user(1, "работает")
    db.set_chats(1, [(-100, "Чат")])
    db.update_user(1, state="ready")

    db.create_user(2, "на паузе")
    db.set_chats(2, [(-200, "Чат")])
    db.update_user(2, state="ready", paused=1)

    db.create_user(3, "без чатов")
    db.update_user(3, state="ready")

    assert [u.telegram_id for u in db.ready_users()] == [1]


def test_счётчик_сбоев_растёт_подряд_и_сбрасывается_успехом():
    db.create_user(1, "petya")
    assert db.note_failure(1, "первый") == 1
    assert db.note_failure(1, "второй") == 2
    assert db.get_user(1).last_error == "второй"

    db.note_success(1)
    assert db.get_user(1).failures == 0
    assert db.get_user(1).last_error is None


def test_stop_уносит_всё_про_человека():
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "Чат")])
    db.save_calendar(1, -100, [{"date": today(1), "what": "собрание"}])
    db.save_photo_note(1, 77, "расписание")
    db.note_activity(1, -100, 12)
    db.note_usage(1, "digest", {"calls": 1, "prompt": 10, "completion": 2})

    db.delete_user(1)

    assert db.get_user(1) is None
    assert db.calendar_on(1, today(1)) == []
    assert db.get_photo_note(1, 77) is None
    assert db.activity_baseline(1, -100) == (0.0, 0)


# --- Календарь ---


def test_повтор_формулировки_обновляет_а_не_плодит():
    db.create_user(1, "petya")
    db.save_calendar(1, -100, [{"date": today(1), "time": "", "what": "Линейка в школе"}])
    db.save_calendar(1, -100, [{"date": today(1), "time": "09:00", "what": "линейка в школе"}])

    rows = db.calendar_on(1, today(1))
    assert len(rows) == 1, "тот же текст другим регистром должен считаться тем же событием"
    assert rows[0]["when"] == "09:00", "верна последняя формулировка"


def test_ключ_события_различает_кириллицу_по_регистру():
    """SQLite lower() кириллицу не трогает — ключ считается в Python, и это важно."""
    assert db._key("Математика, каб. 8") == db._key("математика каб 8")


def test_replace_calendar_чистит_накопленное_но_помнит_когда_сказали():
    db.create_user(1, "petya")
    db.save_calendar(
        1, -100,
        [
            {"date": today(1), "what": "линейка в 9 утра"},
            {"date": today(1), "what": "линейка, приходить к 8:45"},
            {"date": today(1), "what": "форма парадная на линейку"},
        ],
    )
    born = db.calendar_on(1, today(1))[0]["first_seen"]

    db.replace_calendar(1, -100, today(1), [{"time": "09:00", "what": "линейка, форма парадная"}])

    rows = db.calendar_on(1, today(1))
    assert len(rows) == 1
    assert rows[0]["what"] == "линейка, форма парадная"
    assert rows[0]["first_seen"] == born, "дата первого упоминания — про событие, а не про формулировку"


def test_календарь_на_дату_знает_название_чата():
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "5 «З»")])
    db.save_calendar(1, -100, [{"date": today(1), "what": "собрание"}])
    assert db.calendar_on(1, today(1))[0]["title"] == "5 «З»"


def test_события_без_времени_идут_после_событий_со_временем():
    db.create_user(1, "petya")
    db.save_calendar(
        1, -100,
        [
            {"date": today(1), "what": "принести тетрадь"},
            {"date": today(1), "time": "09:00", "what": "линейка"},
        ],
    )
    assert [r["when"] for r in db.calendar_on(1, today(1))] == ["09:00", ""]


def test_календарь_вперёд_не_тянет_прошлое_и_далёкое():
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "Чат")])
    db.save_calendar(
        1, -100,
        [
            {"date": today(-3), "what": "уже прошло"},
            {"date": today(2), "what": "скоро"},
            {"date": today(40), "what": "далеко"},
        ],
    )
    assert [r["what"] for r in db.calendar_ahead(1)] == ["скоро"]


def test_удаление_чата_уносит_его_календарь():
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "Первый"), (-200, "Второй")])
    db.save_calendar(1, -100, [{"date": today(1), "what": "из первого"}])
    db.save_calendar(1, -200, [{"date": today(1), "what": "из второго"}])

    db.remove_chat(1, -100)

    assert [r["what"] for r in db.calendar_on(1, today(1))] == ["из второго"]


# --- Замеры, расход, картинки ---


def test_средняя_оживлённость_считается_по_замерам():
    db.create_user(1, "petya")
    for count in (10, 20, 30):
        db.note_activity(1, -100, count)
    mean, samples = db.activity_baseline(1, -100)
    assert (mean, samples) == (20.0, 3)


def test_расход_без_обращений_не_записывается():
    db.create_user(1, "petya")
    db.note_usage(1, "digest", {"calls": 0, "prompt": 0, "completion": 0})
    assert db.usage_report() == []


def test_расход_складывается_по_видам_работы():
    db.create_user(1, "petya")
    db.note_usage(1, "digest", {"calls": 2, "prompt": 100, "completion": 20})
    db.note_usage(1, "question", {"calls": 1, "prompt": 50, "completion": 10})

    by_kind = {row["kind"]: row["prompt"] for row in db.usage_by_kind()}
    assert by_kind == {"digest": 100, "question": 50}
    assert db.usage_report()[0]["calls"] == 3


def test_пустое_описание_картинки_тоже_запоминается():
    """Иначе за один и тот же букет платим каждую сводку."""
    db.create_user(1, "petya")
    assert db.get_photo_note(1, 7) is None
    db.save_photo_note(1, 7, "")
    assert db.get_photo_note(1, 7) == "", "пустая строка — это «смотрели, читать нечего»"


# --- Приглашения и журнал ---


def test_код_приглашения_одноразовый():
    db.add_invite("abc", created_by=900)
    assert db.use_invite("abc", 1) is True
    assert db.use_invite("abc", 2) is False
    assert db.free_invites() == []


def test_несуществующий_код_не_пускает():
    assert db.use_invite("нет-такого", 1) is False


def test_события_свежие_сверху_и_фильтруются_по_человеку():
    db.create_user(1, "petya")
    db.log_event(1, "logged_in")
    db.log_event(2, "chats_set")
    db.log_event(1, "digest_sent", "5 «З»: 12 сообщений")

    assert [e["kind"] for e in db.recent_events()] == ["digest_sent", "chats_set", "logged_in"]
    assert [e["kind"] for e in db.recent_events(telegram_id=1)] == ["digest_sent", "logged_in"]
    assert db.recent_events(telegram_id=1)[0]["username"] == "petya"


def test_служебное_событие_живёт_без_пользователя():
    db.log_event(None, "version_stale", "26.30.1")
    assert db.last_event_at("version_stale") is not None
    assert db.last_event_at("никогда-не-было") is None
