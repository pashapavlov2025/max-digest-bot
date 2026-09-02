"""
Обращение к модели.

По умолчанию Kimi (OpenAI-совместимый API). GigaChat подключён запасным:
у него ключ меняется на 30-минутный токен, хосты подписаны сертификатами
Минцифры, и он не принимает json_schema — структуру просим словами.
"""

import contextvars
import json
import logging
import re
import time
import uuid
from pathlib import Path

import httpx

from .config import config

log = logging.getLogger(__name__)

RUSSIAN_CA = str(Path(__file__).parent / "russian_ca.pem")
GIGACHAT_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

_giga_token: tuple[str, float] | None = None

# Счётчик токенов текущей операции. Обращений к модели на одну сводку несколько
# — сама сводка, склейка по каждой дате, описание каждой картинки, — и чтобы
# понять, кто сколько потратил, их надо складывать по ходу дела.
_tally: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_tally", default=None)


def start_tally() -> dict:
    """Открывает счёт на текущую операцию и возвращает его же для чтения."""
    tally = {"calls": 0, "prompt": 0, "completion": 0}
    _tally.set(tally)
    return tally


def record(usage: dict | None) -> None:
    """Прибавляет расход одного обращения. Вне открытого счёта — ничего не делает."""
    tally = _tally.get()
    if tally is None:
        return
    tally["calls"] += 1
    if usage:
        tally["prompt"] += int(usage.get("prompt_tokens") or 0)
        tally["completion"] += int(usage.get("completion_tokens") or 0)


async def _kimi(system: str, prompt: str, json_mode: bool) -> str:
    payload = {
        "model": config.kimi_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # У kimi-k2.x размышления включены по умолчанию и утраивают время ответа.
    # Для сводки они не нужны: задача не на рассуждение, а на выборку фактов.
    if config.kimi_thinking is False and config.kimi_model.startswith("kimi-k2"):
        payload["thinking"] = {"type": "disabled"}

    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(
            f"{config.kimi_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.kimi_api_key}"},
            json=payload,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Kimi ответил {response.status_code}: {response.text[:300]}")
    body = response.json()
    record(body.get("usage"))
    return body["choices"][0]["message"]["content"]


async def _gigachat_token() -> str:
    global _giga_token
    if _giga_token and _giga_token[1] > time.time() + 60:
        return _giga_token[0]

    key = config.gigachat_auth_key
    if not key:
        raise RuntimeError("Не задан GIGACHAT_AUTH_KEY")

    async with httpx.AsyncClient(timeout=60, verify=RUSSIAN_CA) as client:
        response = await client.post(
            GIGACHAT_OAUTH,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {key}",
            },
            data={"scope": "GIGACHAT_API_PERS"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"GigaChat не выдал токен ({response.status_code}): {response.text[:200]}")

    data = response.json()
    _giga_token = (data["access_token"], data["expires_at"] / 1000)
    return _giga_token[0]


async def _gigachat(system: str, prompt: str, json_mode: bool) -> str:
    token = await _gigachat_token()
    payload = {
        "model": config.gigachat_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=180, verify=RUSSIAN_CA) as client:
        response = await client.post(
            GIGACHAT_API,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code != 200:
        raise RuntimeError(f"GigaChat ответил {response.status_code}: {response.text[:300]}")
    body = response.json()
    record(body.get("usage"))
    return body["choices"][0]["message"]["content"]


async def complete(system: str, prompt: str, json_mode: bool = False, provider: str | None = None) -> str:
    """Спрашивает модель. Провайдер можно переопределить для конкретного пользователя."""
    name = (provider or config.llm_provider).lower()
    if name == "gigachat":
        return await _gigachat(system, prompt, json_mode)
    return await _kimi(system, prompt, json_mode)


def extract_json(text: str) -> dict:
    """Модели любят заворачивать JSON в markdown-заборчик — снимаем его."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = (fenced.group(1) if fenced else text).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start : end + 1]
    return json.loads(candidate)
