"""
Админские команды.

Смотрят на сервис снаружи: кто где застрял, что у людей происходило
и во сколько это обошлось. Ошибка здесь не видна пользователю — она видна
тем, что админ принимает решения по неверной картинке.
"""

from app import db
from app.bot import admin


# --- Место в воронке ---


def test_воронка_по_фактам_а_не_по_колонке_state(monkeypatch):
    """Колонка откатывается на «ждёт код» при каждом запросе и потом врёт."""
    on_disk = {"session": False}
    monkeypatch.setattr(admin.crypto, "has_session", lambda uid: on_disk["session"])

    assert admin._where(db.create_user(1, "petya")) == "не начал подключение MAX"

    db.update_user(1, phone="+79990001122", state="code")
    assert admin._where(db.get_user(1)) == "ввёл номер, ждёт код"

    # Вход прошёл, но колонка осталась на «ждёт код» — так и было у застрявшего друга
    on_disk["session"] = True
    assert admin._where(db.get_user(1)) == "вошёл, не выбрал чат", "сессия на диске важнее колонки"

    db.set_chats(1, [(-100, "Чат")])
    assert admin._where(db.get_user(1)) == "выбрал чаты, не назначил время"

    db.update_user(1, state="ready")
    assert admin._where(db.get_user(1)) == "работает"


def test_без_сессии_и_с_номером_человек_ждёт_код(monkeypatch):
    monkeypatch.setattr(admin.crypto, "has_session", lambda uid: False)
    db.create_user(1, "petya")
    db.update_user(1, phone="+79990001122")
    assert admin._where(db.get_user(1)) == "ввёл номер, ждёт код"


# --- Журнал и деньги ---


def test_все_события_кода_переведены_на_человеческий():
    """Админу «login_failed» ни о чём не говорит."""
    written = {
        "invite_used", "code_requested", "login_failed", "logged_in", "chats_failed",
        "chats_set", "stopped", "digest_sent", "digest_quiet", "digest_skipped",
        "morning_sent", "burst_digest", "question_asked", "photos_sent",
    }
    assert written <= set(admin.EVENT_NAMES)


def test_деньги_молчат_без_заданной_цены(monkeypatch):
    assert admin._money(1_000_000, 1_000_000) == ""


def test_деньги_считаются_когда_цена_задана(monkeypatch):
    monkeypatch.setenv("PRICE_IN", "2")
    monkeypatch.setenv("PRICE_OUT", "10")
    assert admin._money(1_000_000, 1_000_000) == " ≈ $12.00"
