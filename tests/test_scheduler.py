"""
Что происходит само: пороги всплеска, тихие часы и пауза после срабатывания.

Пороги важнее, чем кажется: спам убьёт доверие к сервису быстрее, чем
пропущенная новость. Поэтому проверяем, что обычный оживлённый вечер
под внеочередную сводку не попадает.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import db, scheduler


@pytest.fixture
def sent(monkeypatch) -> list:
    """Перехватывает внеочередные сводки."""
    bursts = []

    async def send_burst(bot, user, chat, hours=3):
        bursts.append(chat.title)
        return True

    monkeypatch.setattr(scheduler.service, "send_burst", send_burst)
    return bursts


def counts(monkeypatch, **by_chat):
    async def count_recent(telegram_id, phone, chat_ids, minutes):
        return {int(k): v for k, v in by_chat.items()}

    monkeypatch.setattr(scheduler.max_client, "count_recent", count_recent)


def at_hour(monkeypatch, hour: int):
    """Подменяет «сейчас» указанным часом."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 11, hour, 30, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", Clock)


# --- Тихие часы ---


@pytest.mark.parametrize("hour, quiet", [(23, True), (2, True), (7, True), (8, False), (14, False), (22, False)])
def test_ночью_не_тревожим(monkeypatch, hour, quiet):
    """Окно задано через полночь — на этом легко ошибиться."""
    at_hour(monkeypatch, hour)
    assert scheduler._quiet_now() is quiet


def test_окно_внутри_суток_тоже_работает(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "13-15")
    at_hour(monkeypatch, 14)
    assert scheduler._quiet_now() is True
    at_hour(monkeypatch, 16)
    assert scheduler._quiet_now() is False


# --- Пауза после срабатывания ---


def make_user(last_burst_at=None) -> db.User:
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "5 «З»")])
    db.update_user(1, phone="+7", state="ready", last_burst_at=last_burst_at)
    return db.get_user(1)


def test_без_прошлых_всплесков_паузы_нет():
    assert scheduler._cooling_down(make_user()) is False


def test_сразу_после_всплеска_пауза_держится():
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert scheduler._cooling_down(make_user(recent)) is True


def test_через_положенные_часы_пауза_снимается():
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    assert scheduler._cooling_down(make_user(long_ago)) is False


def test_время_без_пояса_считаем_всемирным():
    """Записи старого формата не должны навсегда включать паузу."""
    naive = (datetime.now(timezone.utc) - timedelta(hours=10)).replace(tzinfo=None).isoformat()
    assert scheduler._cooling_down(make_user(naive)) is False


def test_испорченная_отметка_не_ломает_проверку():
    assert scheduler._cooling_down(make_user("позавчера")) is False


# --- Порог всплеска ---


async def test_тихий_чат_всплеском_не_считается(monkeypatch, bot, sent):
    user = make_user()
    counts(monkeypatch, **{"-100": 5})

    await scheduler._check_user(bot, user)

    assert sent == []
    assert db.activity_baseline(1, -100)[1] == 1, "замер всё равно записан"


async def test_много_сообщений_без_истории_дают_сводку(monkeypatch, bot, sent):
    """Пока замеров мало, судим только по абсолютному порогу."""
    user = make_user()
    counts(monkeypatch, **{"-100": 40})

    await scheduler._check_user(bot, user)

    assert sent == ["5 «З»"]


async def test_оживлённый_но_обычный_чат_не_тревожит(monkeypatch, bot, sent):
    user = make_user()
    for _ in range(scheduler.ENOUGH_SAMPLES):
        db.note_activity(1, -100, 30)
    counts(monkeypatch, **{"-100": 40})

    await scheduler._check_user(bot, user)

    assert sent == [], "для этого чата сорок сообщений в час — норма"


async def test_кратный_скачок_на_знакомом_чате_тревожит(monkeypatch, bot, sent):
    user = make_user()
    for _ in range(scheduler.ENOUGH_SAMPLES):
        db.note_activity(1, -100, 5)
    counts(monkeypatch, **{"-100": 60})

    await scheduler._check_user(bot, user)

    assert sent == ["5 «З»"]


async def test_во_время_паузы_всплеск_пропускается(monkeypatch, bot, sent):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    user = make_user(recent)
    counts(monkeypatch, **{"-100": 60})

    await scheduler._check_user(bot, user)

    assert sent == []


async def test_за_раз_тревожим_только_одним_чатом(monkeypatch, bot, sent):
    db.create_user(1, "petya")
    db.set_chats(1, [(-100, "Первый"), (-200, "Второй")])
    db.update_user(1, phone="+7", state="ready")
    user = db.get_user(1)
    counts(monkeypatch, **{"-100": 60, "-200": 60})

    await scheduler._check_user(bot, user)

    assert sent == ["Первый"], "остальные чаты подождут вечера"
