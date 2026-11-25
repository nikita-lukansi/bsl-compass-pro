# -*- coding: utf-8 -*-
"""compass-82a: проверка актуальности версии (контур vs GitHub main).

Контракт update-блока (общий с frontend-агентом compass-4vt):
{status, current, latest, up_to_date, checked_at, detail?}. Сеть везде
замокана (urlopen/HTTPError/TimeoutError) — тесты герметичны.
"""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import update_check  # noqa: E402

FULL_SHA = "AbCdEf1234567890abcd"  # верхний регистр HEX — GitHub так может
SHORT_SHA = "abcdef1"
LATEST_SHORT = FULL_SHA[:7]  # "AbCdEf1" — как отдал GitHub


class _FakeResp:
    """Контекст-менеджер вместо реального HTTP-ответа."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Герметичность: чистый кэш/флаги модуля и отсутствие env-примесей."""
    monkeypatch.delenv("COMPASS_GIT_SHA", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("UPDATE_CHECK", raising=False)
    saved = (update_check._cache, update_check._cache_at, update_check._started)
    update_check._cache = None
    update_check._cache_at = 0.0
    update_check._started = False
    yield
    update_check._cache, update_check._cache_at, update_check._started = saved


def _mock_network(monkeypatch, payload=None, error=None, seen=None):
    """Мок urlopen: считает вызовы, отдаёт payload или бросает error.
    seen (если задан) собирает заголовки запросов."""
    def fake_urlopen(req, timeout=None):
        if seen is not None:
            seen.append(dict(req.headers))
        if error is not None:
            raise error
        return _FakeResp(payload)
    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)


# --------------------------------------------------------------- статусы

def test_ok_up_to_date(monkeypatch):
    """SHA совпадает (регистр HEX не важен): ok + up_to_date True."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA.upper())
    _mock_network(monkeypatch, payload={"sha": FULL_SHA})
    out = update_check.check_update()
    assert out["status"] == "ok"
    assert out["current"] == SHORT_SHA.upper()
    assert out["latest"] == LATEST_SHORT
    assert out["up_to_date"] is True
    assert out["checked_at"]
    assert "detail" not in out


def test_ok_outdated(monkeypatch):
    """Контур отстаёт от main: ok + up_to_date False."""
    monkeypatch.setenv("COMPASS_GIT_SHA", "aaa0001")
    _mock_network(monkeypatch, payload={"sha": FULL_SHA})
    out = update_check.check_update()
    assert out["status"] == "ok"
    assert out["latest"] == LATEST_SHORT
    assert out["up_to_date"] is False


def test_dev_build_no_network(monkeypatch):
    """Хост-запуск (COMPASS_GIT_SHA=dev): честный dev, сеть не трогаем."""
    seen = []
    _mock_network(monkeypatch, payload={"sha": FULL_SHA}, seen=seen)
    out = update_check.check_update()
    assert out["status"] == "dev"
    assert out["current"] == "dev"
    assert out["up_to_date"] is None
    assert "GITHUB_TOKEN" in out["detail"] or "dev" in out["detail"]
    assert seen == []  # сеть не вызывалась


def test_private_401_token_hint(monkeypatch):
    """Приватный репо без токена (401): подсказка про GITHUB_TOKEN."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    err = urllib.error.HTTPError(
        "https://api.github.com/x", 401, "Unauthorized", None, None)
    _mock_network(monkeypatch, error=err)
    out = update_check.check_update()
    assert out["status"] == "private"
    assert out["up_to_date"] is None
    assert "GITHUB_TOKEN" in out["detail"]
    assert "fine-grained" in out["detail"]


def test_private_404(monkeypatch):
    """404 — тот же класс (репо приватное): private, не unavailable."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    err = urllib.error.HTTPError(
        "https://api.github.com/x", 404, "Not Found", None, None)
    _mock_network(monkeypatch, error=err)
    out = update_check.check_update()
    assert out["status"] == "private"


def test_unavailable_timeout(monkeypatch):
    """Таймаут сети: unavailable + detail с причиной."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    _mock_network(monkeypatch, error=TimeoutError("timed out"))
    out = update_check.check_update()
    assert out["status"] == "unavailable"
    assert "сеть недоступна" in out["detail"]


def test_unavailable_url_error(monkeypatch):
    """URLError (нет DNS/маршрута): unavailable, ядро не падает."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    _mock_network(monkeypatch,
                  error=urllib.error.URLError("name resolution failed"))
    out = update_check.check_update()
    assert out["status"] == "unavailable"


def test_token_sent_in_headers(monkeypatch):
    """GITHUB_TOKEN задан → Authorization: Bearer уходит в запрос."""
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    seen = []
    _mock_network(monkeypatch, payload={"sha": FULL_SHA}, seen=seen)
    update_check.check_update()
    assert seen and seen[0]["Authorization"] == "Bearer ghp_test123"
    # User-Agent обязателен для GitHub API
    assert seen[0]["User-agent"] == "bsl-compass-pro"


# --------------------------------------------------------- кэш и выключатель

def test_cache_interval_no_refetch_in_window(monkeypatch):
    """Кэш-интервал: повторный _tick в окне 900 c не ходит в сеть.

    Б1-причина 2: monotonic на свежей ВМ стартует около нуля — «обнулить
    метку» мало (окно не истекает). Время управляется моком monotonic в
    модуле-потребителе, метка не трогается."""
    fake_now = {"t": 1_000_000.0}
    monkeypatch.setattr(update_check.time, "monotonic",
                        lambda: fake_now["t"])
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    calls = []
    _mock_network(monkeypatch, payload={"sha": FULL_SHA}, seen=calls)
    update_check._tick()
    assert len(calls) == 1
    update_check._tick()  # кэш свежий — без сети
    assert len(calls) == 1
    update_check.update_status()  # статус-блок тоже из кэша
    assert len(calls) == 1
    # кэш устарел → снова сеть
    fake_now["t"] += update_check.CHECK_INTERVAL + 1.0
    update_check._tick()
    assert len(calls) == 2


def test_update_status_pending_before_first_check(monkeypatch):
    """До первого фонового чека — служебный pending, поля контракта на месте."""
    out = update_check.update_status()
    assert out["status"] == "pending"
    assert out["current"] == "dev"
    assert out["up_to_date"] is None
    assert out["checked_at"] is None
    assert set(out) >= {"status", "current", "latest", "up_to_date",
                        "checked_at"}


def test_off_switch_disables_everything(monkeypatch):
    """UPDATE_CHECK=off: статус off, фоновый поток не поднимается."""
    monkeypatch.setenv("UPDATE_CHECK", "off")
    assert update_check.enabled() is False
    started_threads = []
    monkeypatch.setattr(update_check.threading, "Thread",
                        lambda *a, **kw: started_threads.append(kw) or None)
    assert update_check.start_background() is False
    assert started_threads == []
    out = update_check.update_status()
    assert out["status"] == "off"
    assert "UPDATE_CHECK=off" in out["detail"]


def test_off_switch_variant_values(monkeypatch):
    """0/false/no — тоже выключение; on/прочее — включено."""
    for val in ("0", "false", "no", "OFF"):
        monkeypatch.setenv("UPDATE_CHECK", val)
        assert update_check.enabled() is False, val
    for val in ("on", "1", "true", "", "yes"):
        monkeypatch.setenv("UPDATE_CHECK", val)
        assert update_check.enabled() is True, val


def test_start_background_spawns_single_daemon(monkeypatch):
    """Включённая проверка поднимает один daemon-поток (идемпотентно)."""
    created = []

    class _FakeThread:
        def __init__(self, *a, **kw):
            created.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(update_check.threading, "Thread", _FakeThread)
    assert update_check.start_background() is True
    assert update_check.start_background() is True  # повтор — без потока
    assert len(created) == 1
    assert created[0]["daemon"] is True


# ------------------------------------------------------- интеграция (без сети)

def test_runtime_status_carries_update_block(monkeypatch):
    """compass-82a: runtime_status (офлайн-ветка) отдаёт блок update из кэша."""
    from src.facade import runtime_tools
    monkeypatch.setattr(runtime_tools, "_offline_mode", lambda: True)
    monkeypatch.setattr(runtime_tools.offline, "offline_status",
                        lambda: {"tasks": {}})
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    update_check._cache = {"status": "ok", "current": SHORT_SHA,
                           "latest": SHORT_SHA, "up_to_date": True,
                           "checked_at": "2026-11-20T10:00:00+00:00"}
    out = runtime_tools.runtime_status()
    assert out["mode"] == "offline"
    assert out["update"]["status"] == "ok"
    assert out["update"]["up_to_date"] is True


def test_api_status_and_update_check_endpoint(monkeypatch):
    """HTTP-интеграция: /api/status несёт блок update (кэш, без сети);
    /api/update-check — ручная перепроверка, обновляющая кэш."""
    from starlette.testclient import TestClient

    from src import main as main_module
    monkeypatch.setenv("COMPASS_GIT_SHA", SHORT_SHA)
    with TestClient(main_module.mcp.http_app()) as client:
        body = client.get("/api/status").json()
        assert body["update"]["status"] == "pending"  # кэш пуст, сети нет

        _mock_network(monkeypatch, payload={"sha": FULL_SHA})
        resp = client.get("/api/update-check")
        assert resp.status_code == 200
        out = resp.json()
        assert out["status"] == "ok" and out["up_to_date"] is True

        # кэш обновлён — /api/status уже показывает результат
        body = client.get("/api/status").json()
        assert body["update"]["status"] == "ok"
        assert body["update"]["latest"] == LATEST_SHORT


def test_api_update_check_off(monkeypatch):
    """off-контур: эндпоинт не ходит в сеть, отдаёт честный off-блок."""
    from starlette.testclient import TestClient

    from src import main as main_module
    monkeypatch.setenv("UPDATE_CHECK", "off")
    seen = []
    _mock_network(monkeypatch, payload={"sha": FULL_SHA}, seen=seen)
    with TestClient(main_module.mcp.http_app()) as client:
        resp = client.get("/api/update-check")
        assert resp.status_code == 200
        assert resp.json()["status"] == "off"
    assert seen == []


def test_same_commit_prefix_semantics():
    """Короткий SHA против полного — сравнение по префиксу, без регистра."""
    assert update_check._same_commit("ABC1234", "abc1234999999") is True
    assert update_check._same_commit("abc1234", "abc1234") is True
    assert update_check._same_commit("abc1234", "abc9999") is False
    assert update_check._same_commit("", "abc1234") is False
    # текущий SHA длиннее latest — тоже валидное сравнение префикса
    assert update_check._same_commit("abc1234999999", "abc1234") is True
