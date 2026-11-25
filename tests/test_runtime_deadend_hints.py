# -*- coding: utf-8 -*-
"""compass-vlu: рантайм-тупик «online + RUNTIME_URL не задан» — честные пути.

Штатная промежуточная конфигурация, в которой обе дорожки до живой базы
перекрыты. Подсказки обязаны вести к выходу: (1) владельцу — задать
RUNTIME_URL (обработка CompassRuntime в 1С); (2) RUNTIME_MODE=offline —
тогда работает офлайн-конвейер offline_submit → offline_wait. Настройки —
env контейнера, применяются рестартом ядра; новых настроек не вводится.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import runtime_tools as rt  # noqa: E402
from src.runtime.client import RuntimeUnavailable  # noqa: E402


@pytest.fixture
def deadend(monkeypatch):
    """online + RUNTIME_URL не задан — конфигурация-тупик из тест-цикла."""
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    monkeypatch.delenv("RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("RUNTIME_MODE", "online")


def _exit_paths(dump: dict) -> None:
    """Подсказка называет оба пути и способ их применения."""
    hint = dump["hint"]
    assert "RUNTIME_URL" in hint
    assert "RUNTIME_MODE=offline" in hint
    assert "CompassRuntime" in hint
    assert "offline_submit" in hint and "offline_wait" in hint
    assert "рестарт" in hint


def test_data_query_deadend_leads_out(deadend):
    res = rt.data_query("ВЫБРАТЬ 1")
    assert "error" in res and "не задан" in res["error"]
    _exit_paths(res)


def test_offline_submit_deadend_error_not_lie(deadend):
    """Было: info «онлайн-тулы работают напрямую» (ложь — URL не задан)."""
    res = rt.offline_submit([{"method": "data_query",
                              "params": {"query": "ВЫБРАТЬ 1"}}])
    assert "error" in res and "перекрыты" in res["error"]
    _exit_paths(res)


def test_offline_wait_deadend_leads_out(deadend):
    res = rt.offline_wait("task_123")
    assert "error" in res
    _exit_paths(res)


def test_runtime_status_deadend_leads_out(deadend):
    st = rt.runtime_status()
    assert st["mode"] == "online" and st["configured"] is False
    _exit_paths(st)


def test_offline_submit_online_with_url_honest(monkeypatch):
    """online + RUNTIME_URL задан: рантайм рабочий — отказ офлайна честен
    и больше не утверждает «напрямую» без указания на URL."""
    monkeypatch.setenv("RUNTIME_MODE", "online")
    monkeypatch.setenv("RUNTIME_URL", "http://1c:8080/sandbox")
    res = rt.offline_submit([{"method": "ping"}])
    assert res.get("info") and "RUNTIME_URL" in res["info"]
    assert "напрямую" not in res["info"]


def test_runtime_error_reachable_no_hint_spam(monkeypatch):
    """Контур настроен, но недоступен — подсказка про настройку не нужна:
    отказ отдаётся как есть (путь выхода другой)."""
    monkeypatch.setenv("RUNTIME_MODE", "online")
    monkeypatch.setenv("RUNTIME_URL", "http://1c:8080/sandbox")
    # singleton клиента читает env при создании — сбрасываем, чтобы URL
    # был подхвачен (в файле его уже создал ранний тест без URL)
    monkeypatch.setattr("src.runtime.client._client", None)
    res = rt.data_query("ВЫБРАТЬ 1")
    assert "error" in res and "hint" not in res


def test_runtime_error_helper_keeps_verbatim():
    """Хелпер не перевирает текст отказа, добавляя пути выхода."""
    exc = RuntimeUnavailable("RUNTIME_URL не задан: тупик")
    out = rt._runtime_error(exc)
    assert out["error"] == "RUNTIME_URL не задан: тупик"
    _exit_paths(out)
