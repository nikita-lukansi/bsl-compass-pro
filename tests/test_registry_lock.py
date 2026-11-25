# -*- coding: utf-8 -*-
"""Аудит PERF-6: файл-лок реестра — конкурентные RMW без потерянных
апдейтов (потоки одного процесса) и честный отказ при удержании лока.
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Сандбокс состояния: settings-каталог в tmp (пути резолвятся на
    каждый вызов — workspace.py:10-11)."""
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(tmp_path / "Settings"))
    ws.init_files()
    return tmp_path


def test_concurrent_writes_no_lost_update(settings, monkeypatch):
    """N потоков конкуретно добавляют конфигурации: ни одна запись не
    потеряна (гонка load→save раньше затирала чужие апдейты)."""
    monkeypatch.setattr(ws, "_REGISTRY_LOCK_TIMEOUT", 30.0)
    n = 8
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            result = reg.add_config(f"cfg{i}", "")
            if "error" in result:
                errors.append(result["error"])
        except Exception as exc:  # поток не молчит
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    names = sorted(c["short_name"] for c in reg.all_configs())
    assert names == [f"cfg{i}" for i in range(n)]  # все n, ни один не затёрт


def test_busy_refusal_while_held(settings, monkeypatch):
    """Удержание лока → writer получает честный отказ за таймаут."""
    monkeypatch.setattr(ws, "_REGISTRY_LOCK_TIMEOUT", 0.3)
    with ws.registry_lock(timeout=10):
        result = reg.add_config("ut", "")
    assert result == {"error": "реестр занят другим процессом"}
    # после отпускания лока запись проходит
    assert "id" in reg.add_config("ut", "")
