# -*- coding: utf-8 -*-
"""TASK-24: object_overview — табличные части и kinds реквизитов."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def test_overview_shows_table_sections(mini_reader):
    out = tools.object_overview("ОбъектТест")
    assert out["table_sections_total"] == 1
    ts = out["table_sections"][0]
    assert ts["name"] == "Товары"
    assert {c["name"] for c in ts["columns"]} == {"Номенклатура", "Количество"}
    assert out["attributes_kinds"].get("ts_attribute") == 2
