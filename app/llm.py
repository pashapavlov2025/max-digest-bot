"""
Обращение к модели.

По умолчанию Kimi (OpenAI-совместимый API). GigaChat подключён запасным:
у него ключ меняется на 30-минутный токен, хосты подписаны сертификатами
Минцифры, и он не принимает json_schema — структуру просим словами.
"""

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


async def _kimi(system: str, prompt: str, json_mode: bool) -> str:
    payload = {
        "model": config.kimi_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{config.kimi_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.kimi_api_key}"},
            json=payload,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Kimi ответил {response.status_code}: {response.text[:300]}")
    return response.json()["choices"][0]["message"]["content"]


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
    return response.json()["choices"][0]["message"]["content"]


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
