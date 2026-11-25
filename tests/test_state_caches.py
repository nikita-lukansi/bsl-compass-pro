# -*- coding: utf-8 -*-
"""Тесты кэшей производительности: stats_24h журнала и текста workspace."""
import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.journal import Journal  # noqa: E402


def test_journal_stats_cached_until_file_changes(tmp_path, monkeypatch):
    """Повторный stats_24h не читает файл; запись — инвалидирует кэш."""
    j = Journal(directory=tmp_path)
    j.call("a", 1.0, True)
    j.call("b", 1.0, False)
    first = j.stats_24h()
    assert first["calls_24h"] == 2

    calls = []

    def spy_tail(limit=200):
        calls.append(limit)
        return j.__class__.tail(j, limit=limit)

    monkeypatch.setattr(j, "tail", spy_tail)
    for _ in range(5):
        assert j.stats_24h()["calls_24h"] == 2
    assert calls == []  # пять опросов панели — ни одного чтения файла

    j.call("c", 1.0, True)  # файл изменился → mtime другой
    assert j.stats_24h()["calls_24h"] == 3
    assert calls == [5000]


def test_workspace_read_cache_fresh_after_write(tmp_path, monkeypatch):
    """_read отдаёт свежие данные после _write и после прямой записи."""
    # Б1: _write делает mkdir settings_dir() — без env это /data/system
    # (на Linux-раннере PermissionError); песочница = tmp_path
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(tmp_path))
    path = tmp_path / "state.json"
    ws._TEXT_CACHE.clear()

    path.write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert ws._read(path) == {"v": 1}

    path.write_text(json.dumps({"v": 2}), encoding="utf-8")
    assert ws._read(path) == {"v": 2}  # прямой write тоже инвалидирует (mtime)

    ws._write(path, {"v": 3})
    assert ws._read(path) == {"v": 3}


def test_workspace_read_returns_fresh_dicts(tmp_path):
    """Два чтения — два независимых dict: мутация не отравляет кэш."""
    path = tmp_path / "state.json"
    ws._TEXT_CACHE.clear()
    path.write_text(json.dumps({"n": 0}), encoding="utf-8")

    a = ws._read(path)
    a["n"] = 999
    b = ws._read(path)
    assert b == {"n": 0}


def test_workspace_read_missing(tmp_path):
    ws._TEXT_CACHE.clear()
    assert ws._read(tmp_path / "нет.json") == {}


def test_save_load_roundtrip_with_cache(tmp_path):
    """Сквозной сценарий сессий: save_sessions → load_sessions видит новое."""
    ws._TEXT_CACHE.clear()
    monkey_patch = tmp_path / "Settings"
    monkey_patch.mkdir()
    os.environ["COMPASS_SETTINGS_PATH"] = str(monkey_patch)  # изоляция пути
    try:
        ws.save_sessions({"sessions": {"x": {"config_id": "A"}}})
        assert ws.load_sessions()["sessions"]["x"]["config_id"] == "A"
        ws.save_sessions({"sessions": {"x": {"config_id": "B"}}})
        assert ws.load_sessions()["sessions"]["x"]["config_id"] == "B"
    finally:
        os.environ.pop("COMPASS_SETTINGS_PATH", None)
        ws._TEXT_CACHE.clear()
