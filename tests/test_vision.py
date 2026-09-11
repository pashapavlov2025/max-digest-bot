"""
Чтение картинок.

Главное здесь — отбор: замер на живом чате показал, что 22 фотографии из 27
были букетами и снимками с праздника. Остальное — про деньги: описание
однажды прочитанной картинки не должно оплачиваться дважды.
"""

import httpx
import pytest

from app import db, vision


def photo_message(text: str = "", photo_id: int = 7, minutes: int = 0) -> dict:
    return {
        "time": 1_757_000_000 + minutes * 60,
        "author": "Ирина",
        "text": text or "[фото]",
        "caption": text,
        "photos": [(photo_id, f"https://max.test/{photo_id}")],
    }


def plain(text: str, minutes: int = 0) -> dict:
    return {"time": 1_757_000_000 + minutes * 60, "author": "Пётр", "text": text,
            "caption": text, "photos": []}


# --- Кого вообще смотреть ---


def test_фото_с_подписью_смотрим():
    assert vision._choose([photo_message("такую тетрадь надо?")]) == [0]


def test_фото_без_подписи_и_вопроса_не_смотрим():
    assert vision._choose([photo_message()]) == []


def test_вопрос_следом_вытягивает_предыдущую_картинку():
    messages = [photo_message(), plain("это технология или ин.яз?", minutes=2)]
    assert vision._choose(messages) == [0]


def test_вопрос_привязывается_только_к_ближайшей_картинке():
    """Иначе один вопрос вытянет на просмотр всю пачку праздничных снимков."""
    messages = [
        photo_message(photo_id=1),
        photo_message(photo_id=2, minutes=1),
        plain("а это что?", minutes=2),
    ]
    assert vision._choose(messages) == [1]


def test_поздний_вопрос_картинку_не_вытягивает():
    late = vision.QUESTION_WINDOW // 60 + 5
    messages = [photo_message(), plain("а что там было?", minutes=late)]
    assert vision._choose(messages) == []


def test_второй_вопрос_подряд_ничего_не_вытягивает():
    messages = [photo_message(), plain("это что?", minutes=1), plain("а размер?", minutes=2)]
    assert vision._choose(messages) == [0]


# --- Причёсывание ответа ---


def test_разметка_и_переносы_из_описания_убираются():
    assert vision._tidy("**Расписание**\n\nна  среду") == "Расписание на среду"


def test_слишком_длинное_описание_обрезается_по_слову():
    note = vision._tidy("слово " * 200)
    assert len(note) <= vision.MAX_NOTE + 1
    assert note.endswith("…")


@pytest.mark.parametrize("answer", ["НЕТ", "нет", "НЕТ (это букет)", "  нет."])
def test_отказ_модели_считается_пустотой(answer):
    assert vision.NOTHING.match(answer)


def test_описание_начатое_не_с_нет_остаётся():
    assert not vision.NOTHING.match("Расписание на среду")


# --- Деньги и кэш ---


async def test_выключенное_зрение_не_ходит_никуда(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "0")
    assert await vision.enrich(1, [photo_message("расписание")]) == 0


async def test_описание_дописывается_к_тексту_сообщения(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "1")
    monkeypatch.setattr(vision, "_describe", _describes("расписание на среду"))

    messages = [photo_message("вот расписание")]
    assert await vision.enrich(1, messages) == 1
    assert messages[0]["text"].endswith("[на фото: расписание на среду]")


async def test_пустое_описание_текст_не_трогает(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "1")
    monkeypatch.setattr(vision, "_describe", _describes(""))

    messages = [photo_message("вот букет")]
    assert await vision.enrich(1, messages) == 0
    assert messages[0]["text"] == "вот букет"


async def test_потолок_картинок_на_сводку_соблюдается(monkeypatch):
    monkeypatch.setenv("VISION_ENABLED", "1")
    monkeypatch.setenv("VISION_MAX_PHOTOS", "2")
    seen = []

    async def describe(telegram_id, photo_id, url):
        seen.append(photo_id)
        return "что-то"

    monkeypatch.setattr(vision, "_describe", describe)
    await vision.enrich(1, [photo_message("подпись", photo_id=i, minutes=i) for i in range(5)])
    assert len(seen) == 2


async def test_знакомая_картинка_второй_раз_не_оплачивается(monkeypatch):
    """Пустой ответ тоже помним: незачем платить дважды за один и тот же букет."""
    db.save_photo_note(1, 7, "")
    monkeypatch.setenv("VISION_ENABLED", "1")

    # Сеть перекрыта фикстурой no_network: если полезем в модель, тест упадёт
    assert await vision._describe(1, 7, "https://max.test/7") == ""


def _describes(note: str):
    async def describe(telegram_id, photo_id, url):
        return note

    return describe
