# -*- coding: utf-8 -*-
"""compass-yho (обратка №5, 2026-11-20): эхо config во все результатные
туры — read_function/object_overview/repomap/call_paths/predefined_items/
code_grep/source_grep/help_search несут {config: {id, short_name, version}}
при выбранной активной конфигурации; codesearch в ответ «векторный индекс
пуст» — config + points (агент видит, ЧТО пусто — корень псевдо-бага
обратки: чужой контекст без эха).

Формат: dict-ответы — ключ config верхнего уровня (паттерн 4w7.3),
list-ответы — завершающий элемент-маркер (паттерн _truncated); без
активной конфигурации поле честно опускается.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import help_tools, repomap, semantic_tools, tools  # noqa: E402
from src.registry import add_config  # noqa: E402

_VERSION = "11.5.8"


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Реестр в tmp (паттерн test_oracle_config_badge)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    yield settings_dir
    ws._TEXT_CACHE.clear()


@pytest.fixture
def echo_env(registry_env, monkeypatch, tmp_path):
    """Мини-индекс + активная конфигурация с version (Configuration.xml в
    доме source) — бейдж резолвируется с непустой версией."""
    src_root = tmp_path / "ut_source"
    src_root.mkdir()
    (src_root / "Configuration.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<MetaDataObject>\n"
        " <Properties>\n"
        "  <Name>УТ</Name>\n"
        f"  <Version>{_VERSION}</Version>\n"
        " </Properties>\n"
        "</MetaDataObject>\n", encoding="utf-8")
    cid = add_config("ut", str(src_root), display_name="УТ 11.5",
                     mode="full")["id"]
    ws.save_settings({"default_config_id": cid})
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield {"cid": cid, "short_name": "ut", "src_root": src_root}
    tools.reset_reader()


def _assert_badge(echo_env, badge: dict) -> None:
    assert badge["id"] == echo_env["cid"]
    assert badge["short_name"] == "ut"
    assert badge["version"] == _VERSION
    # compass-sm4: бейдж несёт режим семантики (контракт yho)
    assert badge["mode"] == "full"


# ------------------------------------------------------------------ dict-туры

def test_read_function_carries_config(echo_env):
    out = tools.read_function("ЭкспортнаяФункция", include_body=False)
    assert out["name"] == "ЭкспортнаяФункция"  # сам ответ не изменился
    _assert_badge(echo_env, out["config"])


def test_read_function_not_found_carries_config(echo_env):
    """error-ветка тоже привязана к базе — агент видит, ГДЕ искали."""
    out = tools.read_function("НетТакогоМетода")
    assert "error" in out
    _assert_badge(echo_env, out["config"])


def test_object_overview_carries_config(echo_env):
    out = tools.object_overview("ДокЗаказ")
    assert out["object"] == "ДокЗаказ"
    _assert_badge(echo_env, out["config"])


def test_call_paths_carries_config(echo_env):
    out = tools.call_paths("ЦепочкаА")
    assert out["method"] == "ЦепочкаА"
    _assert_badge(echo_env, out["config"])


def test_call_paths_error_carries_config(echo_env):
    out = tools.call_paths("НетМетода")
    assert "error" in out
    _assert_badge(echo_env, out["config"])


# ------------------------------------------------------------------ list-туры

def test_repomap_carries_config_without_cache_mutation(echo_env):
    """list-ответ — завершающий элемент-маркер; кэш repomap остаётся
    без привязки к контексту (бейдж — в копию)."""
    repomap._cache.clear()
    out = repomap.repomap("ОбъектТест")
    assert isinstance(out, list)
    _assert_badge(echo_env, out[-1]["config"])
    cached = repomap._cache[repomap._cache_key("ОбъектТест")]  # P1-3a
    assert all("config" not in el for el in cached)
    # повторный вызов (из кэша) — бейдж на месте
    again = repomap.repomap("ОбъектТест")
    _assert_badge(echo_env, again[-1]["config"])


def test_predefined_items_list_carries_config(echo_env):
    out = tools.predefined_items("ВидыСубконтоТест")
    assert isinstance(out, list)
    _assert_badge(echo_env, out[-1]["config"])
    assert all("item_name" in el for el in out[:-1])


def test_predefined_items_enum_carries_config(echo_env):
    out = tools.predefined_items("Перечисление.ХозяйственныеОперации")
    assert out["kind"] == "enum_values"
    _assert_badge(echo_env, out["config"])


def test_code_grep_carries_config(echo_env):
    out = tools.code_grep("Экспортн*", limit=10)
    assert isinstance(out, list)
    _assert_badge(echo_env, out[-1]["config"])


def test_help_search_carries_config(echo_env):
    out = help_tools.help_search("корректировка долга")
    assert isinstance(out, list) and out
    _assert_badge(echo_env, out[-1]["config"])


def test_source_grep_carries_config(echo_env):
    bsl = echo_env["src_root"] / "CommonModules" / "Тест" / "Ext" / "Module.bsl"
    bsl.parent.mkdir(parents=True)
    bsl.write_text("Процедура П()\n\tСловоМаркер();\nКонецПроцедуры\n",
                   encoding="utf-8-sig")
    out = asyncio.run(tools.source_grep("Маркер"))
    assert out["total_matches_returned"] == 1
    _assert_badge(echo_env, out["config"])


# ------------------------------------------------------------------ codesearch

def test_codesearch_empty_index_config_and_points(echo_env, monkeypatch):
    """«Векторный индекс пуст» несёт config + points (0): агент видит,
    КАКАЯ конфигурация пуста — псевдо-баг обратки №5 закрыт."""
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setitem(semantic_tools._sem, "collection",
                        SimpleNamespace(count=lambda: 0))
    out = semantic_tools.codesearch("что-нибудь")
    assert "info" in out
    assert out["points"] == 0
    _assert_badge(echo_env, out["config"])


# ------------------------------------------------------- без активной — поля нет

def test_badge_absent_without_active_config(tmp_path, monkeypatch):
    """Без активной конфигурации эха нет ни в dict-, ни в list-ответах.
    cid явно None — герметичность к sessions.json машины (паттерн S3)."""
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    tools.reset_reader()
    try:
        out = tools.read_function("ЭкспортнаяФункция", include_body=False)
        assert "config" not in out
        items = tools.predefined_items("ВидыСубконтоТест")
        assert isinstance(items, list)
        assert all("config" not in el for el in items)
        overview = tools.object_overview("ДокЗаказ")
        assert "config" not in overview
        paths = tools.call_paths("ЦепочкаА")
        assert "config" not in paths
    finally:
        tools.reset_reader()


def test_help_search_badge_absent_without_active_config(tmp_path, monkeypatch):
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    tools.reset_reader()
    try:
        out = help_tools.help_search("корректировка долга")
        assert isinstance(out, list) and out
        assert all("config" not in el for el in out)
    finally:
        tools.reset_reader()
