# -*- coding: utf-8 -*-
"""Тесты extension_map: синтетика на мини-индексе (герметично, A12/P2)
+ честная пустота на мини-индексе без расширений."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


@pytest.fixture
def mini(monkeypatch, tmp_path):
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


def test_overview_and_object(mini):
    r = tools.reader()
    overview = r.extensions_overview()
    assert overview["overrides_total"] == 3
    # R1/P45: заимствования считаются по обоим значениям живого DDL
    # (belonging 'borrowed' и 'adopted')
    by_ext = {e["name"]: e["borrowed"] for e in overview["extensions"]}
    assert by_ext["Расш1"] == 1 and by_ext["Расш2"] == 1 and by_ext["Расш5"] == 1
    # перехваты сидированы на ОбъектТест, заимствования — на ДокЗаказ
    detail = r.overrides_for("ОбъектТест")
    assert {o["extension_name"] for o in detail["overrides"]} == \
        {"Расш1", "Расш2", "Расш5"}
    objs = r.overrides_for("ДокЗаказ")
    assert {o["belonging"] for o in objs["objects"]} == {"borrowed"}
    r.close()


def test_tool_paths(mini):
    """Тул: обзор и детали через фасад на мини-индексе.

    До A12 патчился cfg.Config.index_db — не та точка: reader() зовёт
    active_index_db() напрямую и открывал постороннюю базу."""
    overview = tools.extension_map()
    assert overview["overrides_total"] == 3

    detail = tools.extension_map("ОбъектТест")
    assert detail["overrides"]
    borrowed = tools.extension_map("ДокЗаказ")
    assert borrowed["objects"]

    nothing = tools.extension_map("НетТакогоОбъекта")
    assert nothing[0]["_no_matches"] is True


def test_extension_map_honest_empty(monkeypatch, tmp_path):
    """Индекс без расширений → честный _no_matches с подсказкой."""
    from tests.mini_index import make_mini_db_no_extensions
    db = make_mini_db_no_extensions(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools._reader = None
    res = tools.extension_map()
    assert isinstance(res, list) and res[0]["_no_matches"] is True
    assert "расшир" in res[0]["hint"].lower()
    tools._reader = None
