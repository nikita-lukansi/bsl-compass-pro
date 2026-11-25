# -*- coding: utf-8 -*-
"""Тесты состояния (bootstrap): сохранение, приоритет env, скан хаба."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import state  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """State-файл и хаб в tmp; env-переменные убраны."""
    st = tmp_path / "config.json"
    hub = tmp_path / "dumps"
    monkeypatch.setattr(state, "STATE_FILE", st)
    monkeypatch.setattr(state, "DUMPS_HUB", hub)
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB", "COMPASS_ALIAS"):
        monkeypatch.delenv(var, raising=False)
    state._STATE = {}
    return st, hub


def test_save_load_roundtrip(isolated_state):
    st, _ = isolated_state
    state.save_state({"source_root": "/dumps/ut", "alias": "ut"})
    # перечитываем с чистого импорта состояния
    state._STATE = {}
    loaded = state.load_state()
    assert loaded["source_root"] == "/dumps/ut"
    assert state.active_alias() == "ut"


def test_env_overrides_state(isolated_state, monkeypatch):
    """env приоритетнее состояния — режим разработчика (пути-значения
    инертны: ни один не создаётся на диске)."""
    _, _ = isolated_state
    state.save_state({"source_root": "/dumps/state", "index_db": "/idx/s.db"})
    monkeypatch.setenv("COMPASS_SOURCE_ROOT", "/dumps/env")
    monkeypatch.setenv("COMPASS_INDEX_DB", "/idx/env.db")
    assert state.active_source_root() == Path("/dumps/env")
    assert state.active_index_db() == Path("/idx/env.db")


def test_scan_dumps_filters_valid(isolated_state):
    _, hub = isolated_state
    (hub / "ВалиднаяВыгрузка").mkdir(parents=True)
    (hub / "ВалиднаяВыгрузка" / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    (hub / "ПростоПапка").mkdir(parents=True)  # без Configuration.xml
    found = state.scan_dumps()
    assert [d["alias"] for d in found] == ["ВалиднаяВыгрузка"]
    assert found[0]["source_root"].endswith("ВалиднаяВыгрузка")


def test_build_state_flow(isolated_state):
    state.build_begin()
    assert state.BUILD_STATE["building"] is True
    state.build_finish(0, ["строка " + str(i) for i in range(20)])
    assert state.BUILD_STATE["building"] is False
    assert state.BUILD_STATE["exit_code"] == 0
    assert len(state.BUILD_STATE["tail"]) == 8  # хвост ограничен
