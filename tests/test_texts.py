"""
Тексты.

Telegram разбирает сообщения как HTML и при незакрытом теге не доставляет
сообщение целиком — вместо текста человек видит тишину. Поэтому разметку
всех текстов проверяем скопом, а подстановки — по месту вызова.
"""

import re

import pytest

from app.bot import texts

# Теги, которые Telegram разбирает в наших сообщениях
TAG = re.compile(r"</?([a-z]+)[^>]*>")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

ALL_TEXTS = {
    name: value
    for name, value in vars(texts).items()
    if isinstance(value, str) and name.isupper()
}


def unbalanced(text: str) -> list[str]:
    """Незакрытые или лишние теги."""
    stack: list[str] = []
    for match in TAG.finditer(text):
        tag = match.group(1)
        if match.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                return [tag]
        else:
            stack.append(tag)
    return stack


def test_есть_что_проверять():
    assert len(ALL_TEXTS) > 20


@pytest.mark.parametrize("name", sorted(ALL_TEXTS))
def test_разметка_текста_сбалансирована(name):
    assert unbalanced(ALL_TEXTS[name]) == [], f"в {name} разъехались теги"


@pytest.mark.parametrize("name", sorted(ALL_TEXTS))
def test_в_тексте_нет_забытых_подстановок(name):
    """Фигурная скобка без пары — это забытый .format, и человек увидит «{title}»."""
    text = ALL_TEXTS[name]
    assert text.count("{") == text.count("}"), f"в {name} непарная фигурная скобка"


# --- Подстановки по месту вызова ---


ARGUMENTS = {
    "NOTHING": {"title": "5 «З»", "period": "последние сутки"},
    "ASK_CODE": {"phone": "+79990001122", "digits": "шесть цифр", "limit": ""},
    "CODE_TRIES_LEFT": {"left": 2},
    "LOGIN_FAILED": {"error": "нет связи"},
    "CHOOSE_TIME": {"title": "5 «З»"},
    "CHATS_SAVED": {"title": "5 «З»"},
    "DONE": {"time": "20:00", "morning": "07:30", "title": "5 «З»"},
    "ABOUT_LINE": {"url": "https://example.org"},
    "CHAT_SEARCH_EMPTY": {"query": "пятый"},
    "CHAT_FOUND": {"query": "пятый"},
    "UNFINISHED_TIME": {"title": "5 «З»"},
    "SESSION_BROKEN": {"error": "нет связи"},
    "FAILED": {"place": " по чату «5 «З»»", "advice": "Попробуйте позже"},
    "BURST": {"title": "5 «З»"},
    "MORNING_ON": {"time": "07:30"},
    "ASK_ABOUT_CHAT": {"title": "5 «З»"},
    "ANSWER_EMPTY_CHAT": {"title": "5 «З»"},
    "PHOTOS_HEADER": {"title": "5 «З»", "period": "последние сутки"},
    "PHOTOS_EMPTY": {"title": "5 «З»", "period": "последние сутки"},
}


@pytest.mark.parametrize("name", sorted(ARGUMENTS))
def test_подстановки_совпадают_с_тем_что_даёт_код(name):
    text = ALL_TEXTS[name]
    expected = set(PLACEHOLDER.findall(text))
    assert expected == set(ARGUMENTS[name]), f"в {name} подстановки разошлись с вызовом"
    text.format(**ARGUMENTS[name])  # не должно бросить


def test_все_подставляемые_тексты_перечислены():
    """Новый текст с подстановкой должен попасть в этот список, а не в продакшн."""
    with_holes = {name for name, text in ALL_TEXTS.items() if PLACEHOLDER.search(text)}
    # DIGIT_WORDS и прочее без подстановок сюда не попадает
    assert with_holes - set(ARGUMENTS) == set(), "текст с подстановкой не покрыт тестом"


# --- Экранирование ---


def test_чужой_текст_экранируется():
    assert texts.quote("<b>жирно</b> & опасно") == "&lt;b&gt;жирно&lt;/b&gt; &amp; опасно"


def test_ссылка_на_страницу_дописывается_только_когда_она_есть():
    assert texts.with_about("текст", "") == "текст"
    assert "https://example.org" in texts.with_about("текст", "https://example.org")


@pytest.mark.parametrize("length, word", [(4, "четыре цифры"), (6, "шесть цифр"), (0, "цифры из сообщения")])
def test_число_цифр_кода_называется_словами(length, word):
    """Просить «шесть цифр», когда MAX прислал четыре, — верный способ запутать."""
    assert texts.digits_word(length) == word
