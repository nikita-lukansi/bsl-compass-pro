# -*- coding: utf-8 -*-
"""compass-yho ④ (обратка №5): config_use(wait_warm=true) — блокировать
ответ до готовности семантики (поллинг warm_state — источник тот же, что
у semantic_status); semantic_status — поле ready (краткий булев итог,
согласован с warm_state)."""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import registry_tools  # noqa: E402
from src.facade import semantic_tools as st  # noqa: E402
from src.registry import add_config  # noqa: E402


@pytest.fixture
def use_env(tmp_path, monkeypatch):
    """Реестр в tmp с одной конфигурацией (переключение на неё)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    ws._TEXT_CACHE.clear()
    cid = add_config("CUW", str(tmp_path / "src"))["id"]
    yield {"cid": cid}
    ws._TEXT_CACHE.clear()


def _mock_warming(monkeypatch, result):
    """schedule_warmup → фиксированный ответ (прогрев «идёт»)."""
    monkeypatch.setattr(st, "schedule_warmup", lambda prev, new: dict(result))


def test_wait_warm_false_returns_immediately(use_env, monkeypatch):
    """wait_warm=false (дефолт) — без ожидания: ответ сразу warming."""
    _mock_warming(monkeypatch, {"state": "warming", "config_id":
                                use_env["cid"]})
    t0 = time.monotonic()
    out = registry_tools.config_use(use_env["cid"])
    assert time.monotonic() - t0 < st.WARM_WAIT_INTERVAL
    assert out["warm"]["state"] == "warming"
    assert "waited" not in out["warm"]


def test_wait_warm_true_polls_until_ready(use_env, monkeypatch):
    """wait_warm=true — поллинг до готовности: «прогрев» завершается в
    фоне через задержку, ответ дождается warm."""
    _mock_warming(monkeypatch, {"state": "warming", "config_id":
                                use_env["cid"]})
    monkeypatch.setitem(st._sem, "warm_state",
                        {"cid": use_env["cid"], "state": "warming"})

    def _finish():
        time.sleep(0.5)
        st._sem["warm_state"] = {"cid": use_env["cid"], "state": "warm"}

    threading.Thread(target=_finish, daemon=True).start()
    out = registry_tools.config_use(use_env["cid"], wait_warm=True)
    assert out["active"] == use_env["cid"]
    assert out["warm"]["state"] == "warm"
    assert out["warm"]["waited"] is True
    assert "timeout" not in out["warm"]


def test_wait_warm_skips_when_no_warming(use_env, monkeypatch):
    """Прогрев не идёт (skipped) — ожидания нет, ответ сохранён как есть."""
    monkeypatch.setattr(st, "schedule_warmup",
                        lambda prev, new: {"skipped": "SEMANTIC_WARM=off"})
    calls = []
    monkeypatch.setattr(st, "wait_warm",
                        lambda timeout=None: calls.append(timeout)
                        or {"state": "unknown"})
    out = registry_tools.config_use(use_env["cid"], wait_warm=True)
    assert out["warm"] == {"skipped": "SEMANTIC_WARM=off"}
    assert calls == []   # wait_warm не звался — ждать нечего


def test_wait_warm_timeout_is_honest(monkeypatch):
    """Прогрев не завершается — честная отмена по таймауту (без
    исключения), текущее состояние в ответе."""
    monkeypatch.setitem(st._sem, "warm_state",
                        {"cid": "x", "state": "warming"})
    out = st.wait_warm(timeout=0.3)
    assert out["state"] == "warming"
    assert out["timeout"] is True


def test_wait_warm_non_warming_returns_at_once(monkeypatch):
    monkeypatch.setitem(st._sem, "warm_state",
                        {"cid": "x", "state": "cold"})
    out = st.wait_warm(timeout=5)
    assert out == {"state": "cold", "waited": True}


# ------------------------------------------------------- semantic_status.ready

def test_semantic_status_ready_true_when_warm(monkeypatch):
    monkeypatch.setitem(st._sem, "warm_state", {"cid": "x", "state": "warm"})
    out = st.semantic_status()
    assert out["warm_state"] == "warm"
    assert out["ready"] is True


def test_semantic_status_ready_false_when_cold_or_unknown(monkeypatch):
    monkeypatch.setitem(st._sem, "warm_state", {"cid": "x", "state": "cold"})
    assert st.semantic_status()["ready"] is False
    monkeypatch.setitem(st._sem, "warm_state", None)
    out = st.semantic_status()
    assert out["warm_state"] == "unknown"
    assert out["ready"] is False
