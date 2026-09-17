"""
Присмотр за входом: версия клиента и доставка кодов.

Главное здесь — не шуметь. Тревога, которая приходит каждое утро в пять,
перестаёт читаться на третий день, поэтому проверяем, что об одном и том же
админ слышит один раз, а пользователи не слышат вовсе.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import db, max_client, scheduler


@pytest.fixture
def admin(monkeypatch, user):
    monkeypatch.setenv("ADMIN_IDS", str(user.telegram_id))
    monkeypatch.setenv("WATCHDOG_CANARY_DAYS", "7")
    return user


@pytest.fixture
def newest(monkeypatch):
    versions = ["26.40.0", "26.39.0", "26.38.0"]

    async def fake():
        return list(versions)

    monkeypatch.setattr(max_client, "newest_versions", fake)
    return versions


@pytest.fixture
def probe(monkeypatch):
    """Запрос кода принимается; дошёл ли код — решает тест."""
    state = {"arrived": False, "asked": 0}

    async def probe_code(phone):
        state["asked"] += 1
        return max_client.CodeRequest(length=6, attempts_left=10, wait_ms=60000)

    async def code_arrived(telegram_id, phone, since_ms):
        return state["arrived"]

    monkeypatch.setattr(max_client, "probe_code", probe_code)
    monkeypatch.setattr(max_client, "code_arrived", code_arrived)
    return state


def age_events(days: int):
    """Сдвигает все события в прошлое, будто проверка была давно."""
    past = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        conn.execute("UPDATE events SET at = ?", (past,))


async def test_об_отставании_версии_говорим_один_раз_и_без_звука(bot, admin, newest):
    await scheduler._check_version(bot)
    await scheduler._check_version(bot)

    assert len(bot.messages) == 1
    assert "26.40.0" in bot.messages[0][1]


async def test_новая_сборка_снова_повод_сказать(bot, admin, newest):
    await scheduler._check_version(bot)
    newest.insert(0, "26.41.0")
    await scheduler._check_version(bot)

    assert len(bot.messages) == 2


async def test_свежая_версия_молчит(bot, admin, newest, monkeypatch):
    monkeypatch.setattr(max_client, "APP_VERSION", "26.39.0")
    await scheduler._check_version(bot)
    assert bot.messages == []


async def test_тревогу_о_коде_слышит_только_админ(bot, admin, probe):
    db.create_user(777, "другой родитель")
    db.update_user(777, phone="+79990009999", state="ready")

    await scheduler._check_delivery(bot)

    assert [chat_id for chat_id, *_ in bot.messages] == [admin.telegram_id]
    assert probe["asked"] == 1


async def test_проверка_не_чаще_раза_в_неделю_даже_когда_сломано(bot, admin, probe):
    await scheduler._check_delivery(bot)
    await scheduler._check_delivery(bot)

    assert probe["asked"] == 1


async def test_сломано_повторно_не_тревожим(bot, admin, probe):
    await scheduler._check_delivery(bot)
    age_events(8)
    await scheduler._check_delivery(bot)

    assert probe["asked"] == 2
    assert len(bot.messages) == 1


async def test_о_починке_говорим_один_раз(bot, admin, probe):
    await scheduler._check_delivery(bot)
    age_events(8)
    probe["arrived"] = True
    await scheduler._check_delivery(bot)
    age_events(8)
    await scheduler._check_delivery(bot)

    assert len(bot.messages) == 2
    assert "снова доходят" in bot.messages[1][1]


async def test_проверка_кодом_по_умолчанию_выключена(bot, admin, probe, monkeypatch):
    monkeypatch.delenv("WATCHDOG_CANARY_DAYS")
    await scheduler._check_delivery(bot)
    assert probe["asked"] == 0


async def test_когда_всё_работает_молчим(bot, admin, probe):
    probe["arrived"] = True
    await scheduler._check_delivery(bot)
    assert bot.messages == []
