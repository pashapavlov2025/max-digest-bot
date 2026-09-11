"""
Чтение MAX.

Настоящий MAX сюда не приходит — вместо него двойник из `fakes`, который
повторяет главную особенность живого: не больше сотни сообщений за запрос
и хождение вглубь по курсору. На этом ломались и сводка, и выборка фото.
"""

import time

import httpx
import pytest

from app import max_client
from tests import fakes
from tests.fakes import FakeMax, message, photo


def history(count: int, *, step_minutes: float = 3, with_photos: bool = False) -> list:
    """Лента сообщений вглубь: чем больше номер, тем старше."""
    return [
        message(
            i,
            minutes_ago=i * step_minutes,
            sender=10 + (i % 3),
            text=f"сообщение {i}",
            attaches=[photo(1000 + i)] if with_photos and i % 5 == 0 else None,
        )
        for i in range(count)
    ]


# --- Вложения словами ---


def test_опрос_разбирается_с_вариантами_и_голосами():
    mark = max_client.describe_attachments(
        fakes.MaxMessage(1, 0, 10, attaches=[fakes.poll("Едем на автобусе?", ["да", "нет"], votes=14)])
    )
    assert mark == "[опрос «Едем на автобусе?»; варианты: да / нет; проголосовало 14]"


def test_опрос_без_названия_и_голосов_всё_равно_виден():
    mark = max_client.describe_attachments(fakes.MaxMessage(1, 0, 10, attaches=[fakes.poll("", [])]))
    assert mark == "[опрос «без названия»]"


def test_файл_называется_именем():
    mark = max_client.describe_attachments(fakes.MaxMessage(1, 0, 10, attaches=[fakes.file("расписание.pdf")]))
    assert mark == "[файл «расписание.pdf»]"


def test_несколько_вложений_перечисляются():
    mark = max_client.describe_attachments(
        fakes.MaxMessage(1, 0, 10, attaches=[photo(1), fakes.voice()])
    )
    assert mark == "[фото; голосовое сообщение]"


def test_без_вложений_пометки_нет():
    assert max_client.describe_attachments(fakes.MaxMessage(1, 0, 10)) == ""


def test_ссылки_на_фото_берутся_только_у_фотографий():
    msg = fakes.MaxMessage(1, 0, 10, attaches=[photo(7), fakes.file("а.pdf"), photo(8)])
    assert [pid for pid, _ in max_client.photo_links(msg)] == [7, 8]


# --- Окно истории ---


async def test_окно_идёт_вглубь_страницами(monkeypatch):
    client = fakes.install(monkeypatch, FakeMax(history(120)))
    messages = await max_client.fetch_window(1, "+7", -100, hours=5, limit=500)

    assert client.pages >= 2, "сотня за запрос — значит, за пятью часами надо идти вглубь"
    assert messages == sorted(messages, key=lambda m: m["time"]), "сводке нужен порядок по времени"


async def test_окно_отрезает_всё_старше_границы(monkeypatch):
    fakes.install(monkeypatch, FakeMax(history(120)))
    messages = await max_client.fetch_window(1, "+7", -100, hours=1, limit=500)

    edge = time.time() - 3600
    assert messages, "за час сообщения были"
    assert min(m["time"] for m in messages) >= edge - 5


async def test_потолок_сообщений_обрывает_хождение_вглубь(monkeypatch):
    client = fakes.install(monkeypatch, FakeMax(history(300)))
    await max_client.fetch_window(1, "+7", -100, hours=240, limit=100)
    assert client.pages == 1, "набрали потолок — дальше ходить незачем"


async def test_пустая_история_даёт_пустую_сводку(monkeypatch):
    fakes.install(monkeypatch, FakeMax([]))
    assert await max_client.fetch_window(1, "+7", -100, hours=24, limit=100) == []


async def test_сообщения_без_текста_и_вложений_выбрасываются(monkeypatch):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=1, text="есть текст"),
        message(2, minutes_ago=2, text="   "),
        message(3, minutes_ago=3, text=""),
    ]))
    messages = await max_client.fetch_window(1, "+7", -100, hours=24, limit=100)
    assert [m["text"] for m in messages] == ["есть текст"]


async def test_фото_без_подписи_остаётся_в_сводке_пометкой(monkeypatch):
    """Пустой текст выбрасывается, но «[фото]» — это уже содержание."""
    fakes.install(monkeypatch, FakeMax([message(1, minutes_ago=1, attaches=[photo(7)])]))
    messages = await max_client.fetch_window(1, "+7", -100, hours=24, limit=100)
    assert messages[0]["text"] == "[фото]"
    assert messages[0]["caption"] == ""


async def test_подпись_к_фото_идёт_и_в_текст_и_отдельным_полем(monkeypatch):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=1, text="такую тетрадь?", attaches=[photo(7)])
    ]))
    got = (await max_client.fetch_window(1, "+7", -100, hours=24, limit=100))[0]
    assert got["text"] == "такую тетрадь? [фото]"
    assert got["caption"] == "такую тетрадь?", "по подписи решают, смотреть ли на картинку"


async def test_имена_авторов_подставляются(monkeypatch):
    fakes.install(monkeypatch, FakeMax([message(1, minutes_ago=1, sender=42, text="привет")]))
    got = await max_client.fetch_window(1, "+7", -100, hours=24, limit=100)
    assert got[0]["author"] == "Человек42"


async def test_дубли_страниц_не_задваивают_сообщения(monkeypatch):
    """MAX может отдать то же сообщение на двух страницах — считаем по id."""
    repeated = history(40) * 2
    fakes.install(monkeypatch, FakeMax(repeated))
    messages = await max_client.fetch_window(1, "+7", -100, hours=24, limit=500)
    assert len(messages) == 40


# --- Фотографии ---


async def test_фото_отдаются_последними_и_со_ссылками(monkeypatch):
    fakes.install(monkeypatch, FakeMax(history(60, with_photos=True)))
    photos = await max_client.fetch_photos(1, "+7", -100, hours=24, limit=4)

    assert len(photos) == 4
    assert photos == sorted(photos, key=lambda p: p["time"]), "показываем в порядке появления"
    assert all(p["url"].startswith("https://max.test/") for p in photos)
    assert all(p["author"].startswith("Человек") for p in photos)


async def test_последнее_фото_действительно_самое_свежее(monkeypatch):
    fakes.install(monkeypatch, FakeMax(history(60, with_photos=True)))
    photos = await max_client.fetch_photos(1, "+7", -100, hours=24, limit=3)
    assert photos[-1]["photo_id"] == 1000, "нулевое сообщение — самое свежее"


async def test_подпись_к_фото_едет_вместе_с_ним(monkeypatch):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=5, text="такую тетрадь?", attaches=[photo(7)])
    ]))
    got = await max_client.fetch_photos(1, "+7", -100, hours=24, limit=10)
    assert got[0]["caption"] == "такую тетрадь?"


async def test_в_чате_без_фотографий_ничего_не_находится(monkeypatch):
    fakes.install(monkeypatch, FakeMax(history(20)))
    assert await max_client.fetch_photos(1, "+7", -100, hours=24, limit=10) == []


async def test_фото_старше_окна_не_показываются(monkeypatch):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=10, attaches=[photo(7)]),
        message(2, minutes_ago=60 * 30, attaches=[photo(8)]),
    ]))
    got = await max_client.fetch_photos(1, "+7", -100, hours=24, limit=10)
    assert [p["photo_id"] for p in got] == [7]


async def test_несколько_фото_в_одном_сообщении_считаются_отдельно(monkeypatch):
    fakes.install(monkeypatch, FakeMax([
        message(1, minutes_ago=5, text="вот", attaches=[photo(7), photo(8)])
    ]))
    got = await max_client.fetch_photos(1, "+7", -100, hours=24, limit=10)
    assert [p["photo_id"] for p in got] == [7, 8]


# --- Скачивание ---


def answers(monkeypatch, *, status: int = 200, content: bytes = b"", boom: Exception | None = None):
    """Подменяет ответ сети для одного похода за картинкой."""

    async def handle(self, request):
        if boom:
            raise boom
        return httpx.Response(status, content=content)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)


async def test_картинка_скачивается(monkeypatch):
    answers(monkeypatch, content=b"\xff\xd8jpeg")
    assert await max_client.download_photo("https://max.test/7") == b"\xff\xd8jpeg"


async def test_протухшая_ссылка_возвращает_ничего(monkeypatch):
    answers(monkeypatch, status=403, content=b"expired")
    assert await max_client.download_photo("https://max.test/7") is None


async def test_неподъёмная_картинка_не_качается(monkeypatch):
    # Потолок опускаем, чтобы не гонять десять мегабайт ради одной проверки
    monkeypatch.setattr(max_client, "PHOTO_MAX_BYTES", 1024)
    answers(monkeypatch, content=b"x" * 1025)
    assert await max_client.download_photo("https://max.test/7") is None


async def test_картинка_на_пределе_размера_ещё_проходит(monkeypatch):
    monkeypatch.setattr(max_client, "PHOTO_MAX_BYTES", 1024)
    answers(monkeypatch, content=b"x" * 1024)
    assert await max_client.download_photo("https://max.test/7") is not None


async def test_оборванная_сеть_не_роняет_пересылку(monkeypatch):
    answers(monkeypatch, boom=httpx.ConnectError("сеть отвалилась"))
    assert await max_client.download_photo("https://max.test/7") is None


# --- Список чатов и замер всплеска ---


async def test_список_чатов_отдаёт_живые_сверху_и_без_диалогов(monkeypatch):
    fakes.install(monkeypatch, FakeMax([], chats=[
        fakes.FakeChat(-100, "Тихий чат", last_event=100),
        fakes.FakeChat(-200, "Живой чат", last_event=900),
        fakes.FakeChat(-300, "Личка", kind="DIALOG", last_event=999),
        fakes.FakeChat(-400, "", last_event=500),
    ]))
    chats = await max_client.list_chats(1, "+7")
    assert [c["title"] for c in chats] == ["Живой чат", "Тихий чат"]


async def test_замер_всплеска_считает_каждый_чат_в_одной_сессии(monkeypatch):
    client = fakes.install(monkeypatch, FakeMax([
        message(i, minutes_ago=i * 10, text=f"с {i}") for i in range(12)
    ]))
    counts = await max_client.count_recent(1, "+7", [-100, -200], minutes=30)

    assert counts == {-100: 3, -200: 3}, "за полчаса при шаге 10 минут — три сообщения"
    assert client.asked == [-100, -200], "оба чата спрошены, вход один"
