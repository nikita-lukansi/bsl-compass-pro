# -*- coding: utf-8 -*-
"""Офлайн-гарды рантайм-тулов: единая подсказка на offline_submit (arch §2).

Гард больше не пишет файлы запросов (Т2 фаза 1) — конвейер файлов ведёт
реестр задач tasks.py; агент собирает пакет тулом offline_submit.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import runtime_tools  # noqa: E402
from src.runtime import offline  # noqa: E402


@pytest.fixture
def offline_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_MODE", "offline")
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    return tmp_path


def test_offline_guard_suggests_submit(offline_mode):
    out = runtime_tools.get_object("Справочник", "Контрагенты", "000000001")
    assert "info" in out and "offline_submit" in out["info"]
    # гард не пишет файлы в очередь
    assert not list((offline_mode / "queue").glob("req_*"))
