"""
Разбор сбоев.

Человеку бесполезен текст исключения — ему нужно знать, что делать.
Самое важное здесь: не советовать перезаходить в MAX, когда отвалилась
не сессия, а модель. На живом боте такой совет стоил бы лишних попыток
входа, а кодов MAX даёт мало.
"""

import pytest

from app import errors


@pytest.mark.parametrize(
    "text, kind",
    [
        ("not authorized", "session"),
        ("401 Unauthorized", "session"),
        ("session expired", "session"),
        ("Пользователь не авторизован", "session"),
        ("429 Too Many Requests", "rate_limit"),
        ("превышен лимит", "rate_limit"),
        ("Connection timed out", "network"),
        ("SSL handshake failed", "network"),
        ("чат не найден", "chat_gone"),
        ("совсем непонятно что", "unknown"),
    ],
)
def test_сбой_узнаётся_по_приметам(text, kind):
    assert errors.classify(RuntimeError(text)).kind == kind


def test_сбой_модели_не_принимают_за_отвалившуюся_сессию():
    """«401 invalid token» от Kimi — это не MAX, и /start здесь вредный совет."""
    failure = errors.classify(RuntimeError("401 invalid token"), kind_hint="llm")
    assert failure.kind == "llm"
    assert "/start" not in failure.advice


def test_лимит_модели_остаётся_лимитом():
    assert errors.classify(RuntimeError("429 rate limit"), kind_hint="llm").kind == "rate_limit"


def test_совет_есть_на_любой_вид_сбоя():
    for kind in list(errors.SIGNS) + ["llm", "unknown"]:
        assert errors.Failure(kind=kind, detail="").advice


def test_сами_не_пройдут_только_сессия_и_пропавший_чат():
    assert errors.Failure("session", "").fatal
    assert errors.Failure("chat_gone", "").fatal
    assert not errors.Failure("network", "").fatal
    assert not errors.Failure("llm", "").fatal


def test_подробность_содержит_тип_исключения():
    assert errors.classify(ValueError("что-то")).detail == "ValueError: что-то"


def test_длинная_подробность_обрезается():
    assert len(errors.classify(RuntimeError("х" * 500)).detail) == 300
