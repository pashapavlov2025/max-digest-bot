"""
Разбор сбоев в человеческие слова.

Пользователю бесполезен текст исключения: ему нужно знать, что делать —
подключиться заново, подождать или ничего не делать. Здесь одно место,
где технические ошибки превращаются в такой совет.
"""

from dataclasses import dataclass

# Порядок важен: первое совпадение выигрывает.
# Ключ — вид сбоя, значение — приметы в тексте исключения.
SIGNS = {
    "session": (
        "not authorized", "unauthorized", "auth", "token", "login required",
        "session", "logout", "не авторизован", "401", "403",
    ),
    "rate_limit": ("429", "rate limit", "too many", "quota", "лимит"),
    "network": ("timeout", "timed out", "connection", "connect", "unreachable", "ssl", "dns"),
    "chat_gone": ("not found", "no access", "forbidden", "чат не найден", "404"),
}

ADVICE = {
    "session": (
        "Сессия MAX отвалилась — это бывает, если выйти из аккаунта на всех устройствах "
        "или сменить пароль. Подключимся заново: /start"
    ),
    "rate_limit": (
        "Уперлись в лимит запросов. Ничего делать не надо — следующая сводка придёт "
        "по расписанию. Если нужно срочно, попробуйте через полчаса."
    ),
    "network": (
        "Не достучался до MAX — похоже, временные сетевые неполадки. "
        "Попробую сам в следующий раз, вручную можно повторить через несколько минут."
    ),
    "chat_gone": (
        "Не вижу этот чат в MAX. Возможно, вас из него удалили или он переименован. "
        "Проверить и поправить список — /settings"
    ),
    "llm": (
        "Модель не ответила. Обычно это временно: попробуйте повторить через пару минут. "
        "Если повторяется — напишите админу."
    ),
    "unknown": "Что-то пошло не так на моей стороне. Если повторится — напишите админу.",
}


@dataclass
class Failure:
    kind: str
    detail: str

    @property
    def advice(self) -> str:
        return ADVICE.get(self.kind, ADVICE["unknown"])

    @property
    def fatal(self) -> bool:
        """Сбой, который сам не пройдёт: без вмешательства человека дальше нет смысла."""
        return self.kind in {"session", "chat_gone"}


# Сбой модели разбираем по своим приметам: «401 invalid token» от Kimi — это
# не отвалившаяся сессия MAX, и советовать человеку /start здесь вредно.
LLM_SIGNS = ("429", "rate limit", "too many", "quota", "лимит")


def classify(exc: BaseException, kind_hint: str | None = None) -> Failure:
    text = f"{type(exc).__name__}: {exc}".strip()
    lowered = text.lower()
    detail = text[:300]

    if kind_hint == "llm":
        kind = "rate_limit" if any(sign in lowered for sign in LLM_SIGNS) else "llm"
        return Failure(kind=kind, detail=detail)

    for kind, signs in SIGNS.items():
        if any(sign in lowered for sign in signs):
            return Failure(kind=kind, detail=detail)

    return Failure(kind=kind_hint or "unknown", detail=detail)
