# -*- coding: utf-8 -*-
"""compass-1hh + compass-v77: виды метаданных с одинаковым именем.

modules.object_name хранит имя без вида (classify.rs:39 — object_name =
второй сегмент rel_path) → «Хозрасчетный» на КА — и ПланСчетов, и
РегистрБухгалтерии. Различает их колонка category (первый сегмент пути).
Фикстура: БухгалтерскийГлавный в ChartsOfAccounts (флаги, ТЧ-строк нет) и
AccountingRegisters (измерение + ресурсы).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def mini(monkeypatch, tmp_path):
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


# ------------------------------------------------------- resolver вида

def test_resolve_kind_query():
    assert tools._resolve_kind_query("Хозрасчетный") == ("Хозрасчетный", None)
    assert tools._resolve_kind_query("ПланСчетов.Хозрасчетный") == (
        "Хозрасчетный", "ChartsOfAccounts")
    assert tools._resolve_kind_query("ChartsOfAccounts.Хозрасчетный") == (
        "Хозрасчетный", "ChartsOfAccounts")
    assert tools._resolve_kind_query("РегистрБухгалтерии.Хозрасчетный") == (
        "Хозрасчетный", "AccountingRegisters")
    # неизвестный префикс — не вид, а часть имени: как есть
    assert tools._resolve_kind_query("МойВид.Х") == ("МойВид.Х", None)


def test_categories_of_same_name(mini):
    r = tools.reader()
    cats = tools._categories_of(r, "БухгалтерскийГлавный")
    assert cats == ["AccountingRegisters", "ChartsOfAccounts"]


# --------------------------------------- object_overview (compass-1hh)

def test_overview_disambiguates_same_name(mini):
    """Голое имя двух видов — дизамбигуация с кандидатами, не каша."""
    out = tools.object_overview("БухгалтерскийГлавный")
    assert out.get("_ambiguous") is True
    assert out["object"] == "БухгалтерскийГлавный"
    queries = {c["query"] for c in out["categories"]}
    assert queries == {"AccountingRegisters.БухгалтерскийГлавный",
                       "ChartsOfAccounts.БухгалтерскийГлавный"}
    assert "атрибуты" in out["hint"]


def test_overview_kind_filters_attributes(mini):
    """Запрос с видом: атрибуты только этого вида, category в ответе.
    У плана нет ресурсов регистра — «у плана счетов есть СуммаНУ» больше
    не ложь (1hh)."""
    plan = tools.object_overview("ПланСчетов.БухгалтерскийГлавный")
    assert plan["category"] == "ChartsOfAccounts"
    names = {a["attr_name"] for a in plan["attributes"]}
    assert names == {"Валютный", "Сторно"}
    assert "Сумма" not in names and "Организация" not in names

    reg = tools.object_overview("AccountingRegisters.БухгалтерскийГлавный")
    assert reg["category"] == "AccountingRegisters"
    reg_names = {a["attr_name"] for a in reg["attributes"]}
    assert reg_names == {"Организация", "Сумма", "СуммаНУ"}
    assert {a["attr_kind"] for a in reg["attributes"]} == \
        {"dimension", "resource"}


def test_overview_unique_name_unaffected(mini):
    """Имя одного вида — прежний контракт (с авто-категорией в ответе)."""
    out = tools.object_overview("ОбъектТест")
    assert out.get("_ambiguous") is None and "error" not in out
    assert out["object"] == "ОбъектТест"
    assert out["category"] == "CommonModules"


# ------------------------------------------ verify_field (compass-v77)

def test_verify_field_plan_ts_not_indexed(mini):
    """ВидыСубконто у плана счетов: секции ТЧ в индексе отсутствуют →
    честное None/not_indexed, а не ложное False (v77)."""
    out = tools.verify_field("ПланСчетов.БухгалтерскийГлавный", "ВидыСубконто")
    assert out["holds"] is None
    assert out["verdict"] == "not_indexed"
    assert out["category"] == "ChartsOfAccounts"
    assert "ТЧ" in out["hint"] or "табличной" in out["hint"]


def test_verify_field_bare_name_ts_not_indexed(mini):
    out = tools.verify_field("БухгалтерскийГлавный", "ВидыСубконто")
    assert out["holds"] is None and out["verdict"] == "not_indexed"


def test_verify_field_indexed_field_true_union(mini):
    """Голое имя: поле вида-регистра находится (v77 «Сумма должна
    остаться True»); evidence несёт category вида."""
    out = tools.verify_field("БухгалтерскийГлавный", "Сумма")
    assert out["holds"] is True
    assert out["evidence"]["category"] == "AccountingRegisters"
    flag = tools.verify_field("БухгалтерскийГлавный", "Сторно")
    assert flag["holds"] is True
    assert flag["evidence"]["category"] == "ChartsOfAccounts"


def test_verify_field_kind_scoped_false_absent(mini):
    """Вид + полностью индексированный объект (ТЧ есть) — честное False."""
    bad = tools.verify_field("Документ.ДокЗаказ", "НетТакогоРеквизита")
    assert bad["holds"] is False and bad["verdict"] == "field_not_found"
    assert bad["category"] == "Documents"


def test_verify_field_ts_column_still_true(mini):
    ok = tools.verify_field("ОбъектТест", "Товары")
    assert ok["holds"] is True and ok["kind"] == "table_section"


def test_reader_attributes_expose_category(mini):
    r = tools.reader()
    rows = r.object_attributes("БухгалтерскийГлавный")
    assert {"category", "attr_name", "attr_kind"} <= set(rows[0])
    assert {x["category"] for x in rows} == \
        {"AccountingRegisters", "ChartsOfAccounts"}
