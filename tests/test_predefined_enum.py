# -*- coding: utf-8 -*-
"""compass-4w7.2 (обратная связь 2026-11-15): predefined_items отдаёт
значения перечислений.

Репро на живом КА: predefined_items(«Перечисление.ХозяйственныеОперации»,
query=«Закупка») → [], хотя object_overview отдаёт 816 значений — сборщик
пишет значения в enum_values, а тул читал только predefined. Справочники/
планы — прежний путь без изменений (регрессия).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def test_enum_values_listed(mini_reader):
    """Перечисление с тремя значениями → все значения, kind=enum_values."""
    out = tools.predefined_items("Перечисление.ХозяйственныеОперации")
    assert isinstance(out, dict) and out["kind"] == "enum_values"
    assert [v["name"] for v in out["items"]] == [
        "ПоступлениеДопРасходов", "ПродажаОпераций", "КорректировкаРегистров"]
    assert out["total"] == 3


def test_enum_values_query_filter(mini_reader):
    """query по подстроке имени значения → фильтр (репро обратки)."""
    out = tools.predefined_items("Перечисление.ХозяйственныеОперации",
                                 query="Продажа")
    assert [v["name"] for v in out["items"]] == ["ПродажаОпераций"]
    assert out["query"] == "продажа"


def test_catalog_predefined_unchanged(mini_reader):
    """Регрессия прежнего пути: справочник с предопределёнными —
    список item_name/item_code/item_synonym, не dict-обёртка."""
    out = tools.predefined_items("ВидыСубконтоТест")
    assert isinstance(out, list)
    assert [i["item_name"] for i in out] == ["Субконто1", "Субконто2",
                                             "Субконто3"]


def test_enum_truncation(mini_reader):
    """limit на перечислениях работает как на predefined: срез + маркер."""
    out = tools.predefined_items("Перечисление.ХозяйственныеОперации",
                                 limit=2)
    assert len(out["items"]) == 2 and out["_truncated"] is True
    assert out["total"] == 3


def test_empty_enum_no_error(mini_reader):
    """Перечисление без значений → пустой items, без ошибок."""
    out = tools.predefined_items("Перечисление.ПустоеПеречисление")
    assert out["items"] == [] and out["total"] == 0


def test_non_enum_still_predefined(mini_reader):
    """Объект без вида и без значений enum_values → прежний путь
    (голое имя пробуется читателем enum_values и проваливается вниз)."""
    out = tools.predefined_items("ВидыСубконтоТест")
    assert isinstance(out, list) and out
