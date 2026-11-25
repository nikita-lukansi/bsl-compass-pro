# -*- coding: utf-8 -*-
"""Тесты стека расширений реестра: эффективный порядок v0.5, валидация,
order_confirmed (arch-t3 §4: назначение главнее позиции, 1 = верх списка)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg_mod  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Рабочая папка с реестром в tmp (как в test_registry.py)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB"):
        monkeypatch.delenv(var, raising=False)
    ws._TEXT_CACHE.clear()
    return tmp_path


def test_effective_order_purpose_beats_position(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "Расш5", 1, "Addition")     # Addition, но позиция 1
    reg_mod.stack_add(cid, "Расш1", 3, "Patch")        # Patch — применится ПЕРВЫМ
    reg_mod.stack_add(cid, "Расш2", 1, "Customization")
    names = [e["name"] for e in reg_mod.stack_of(cid)]
    assert names == ["Расш1", "Расш2", "Расш5"]        # назначение главнее позиции
    assert [e["effective_position"] for e in reg_mod.stack_of(cid)] == [1, 2, 3]


def test_effective_order_position_inside_purpose(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "A", 2, "Patch")
    reg_mod.stack_add(cid, "B", 1, "Patch")
    assert [e["name"] for e in reg_mod.stack_of(cid)] == ["B", "A"]


def test_order_not_confirmed_manual(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "A", 1, "Patch")
    entry = reg_mod.stack_of(cid)[0]
    assert entry["order_source"] == "manual"
    # order_confirmed — флаг уровня стека, дублируется в каждой записи
    assert entry.get("order_confirmed") is False


def test_add_requires_position_and_purpose(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    assert "error" in reg_mod.stack_add(cid, "X", 0, "Patch")   # позиция < 1
    assert "error" in reg_mod.stack_add(cid, "X", 1, "НетТакого")
    assert "error" in reg_mod.stack_add(cid, "X", 1, "Patch",
                                        status="wild")
    assert reg_mod.stack_add(cid, "X", 1, "Patch").get("name") == "X"


def test_add_accepts_russian_purpose_synonyms(workspace_dir):
    """Русские синонимы назначения → канон Patch|Customization|Addition."""
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "А", 1, "Исправление")
    reg_mod.stack_add(cid, "Б", 1, "адаптация")
    reg_mod.stack_add(cid, "В", 1, "Дополнение")
    purposes = [e["purpose"] for e in reg_mod.stack_of(cid)]
    assert purposes == ["Patch", "Customization", "Addition"]


def test_add_duplicate_name_rejected(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "Расш1", 1, "Patch")
    assert "error" in reg_mod.stack_add(cid, "Расш1", 2, "Patch")


def test_move_and_status_and_remove(workspace_dir):
    cid = reg_mod.add_config("KA", "ka-src")["id"]
    reg_mod.stack_add(cid, "A", 2, "Patch")
    reg_mod.stack_add(cid, "B", 1, "Patch")
    assert reg_mod.stack_move(cid, "A", 1)["moved"] == "A"
    assert [e["name"] for e in reg_mod.stack_of(cid)] == ["A", "B"]
    assert "error" in reg_mod.stack_move(cid, "A", 0)
    assert reg_mod.stack_set_status(cid, "A", "dev")["status"] == "dev"
    assert reg_mod.stack_of(cid)[0]["status"] == "dev"
    assert reg_mod.stack_remove(cid, "A")["removed"] == "A"
    assert "error" in reg_mod.stack_remove(cid, "A")
