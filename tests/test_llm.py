"""
Обвязка модели: разбор ответа и счёт токенов.

Модели любят заворачивать JSON в markdown-заборчик и добавлять вежливые
пояснения вокруг — разбор должен переживать и то, и другое.
"""

import json

import pytest

from app import llm


def test_голый_json_разбирается():
    assert llm.extract_json('{"headline": "привет"}') == {"headline": "привет"}


def test_заборчик_снимается():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_заборчик_без_подписи_тоже_снимается():
    assert llm.extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_вежливость_вокруг_json_отбрасывается():
    raw = 'Конечно! Вот сводка:\n{"a": 1}\nНадеюсь, помог.'
    assert llm.extract_json(raw) == {"a": 1}


def test_вложенные_скобки_не_обрезаются():
    data = {"events": [{"what": "собрание"}], "noise": "—"}
    assert llm.extract_json(json.dumps(data, ensure_ascii=False)) == data


def test_мусор_вместо_json_честно_ломается():
    with pytest.raises(ValueError):
        llm.extract_json("никакого json тут нет")


# --- Счёт токенов ---


def test_счёт_вне_операции_ничего_не_ломает():
    llm.record({"prompt_tokens": 10, "completion_tokens": 2})  # не должно бросить


def test_обращения_складываются_в_одну_операцию():
    tally = llm.start_tally()
    llm.record({"prompt_tokens": 100, "completion_tokens": 20})
    llm.record({"prompt_tokens": 50, "completion_tokens": 10})

    assert tally == {"calls": 2, "prompt": 150, "completion": 30}


def test_обращение_без_отчёта_о_токенах_всё_равно_считается():
    """Провайдер не обязан прислать usage, но обращение было и его надо видеть."""
    tally = llm.start_tally()
    llm.record(None)
    assert tally == {"calls": 1, "prompt": 0, "completion": 0}


def test_новый_счёт_начинается_с_нуля():
    first = llm.start_tally()
    llm.record({"prompt_tokens": 100, "completion_tokens": 20})
    second = llm.start_tally()
    llm.record({"prompt_tokens": 1, "completion_tokens": 1})

    assert first["prompt"] == 100
    assert second["prompt"] == 1
