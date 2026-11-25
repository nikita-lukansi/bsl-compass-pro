# -*- coding: utf-8 -*-
"""compass-93j: object_overview — фильтр/пагинация секции attributes.

Было: первые 50 из 154 по алфавиту — добор только через verify_field ×N.
Контракт: attr_filter (подстрока/маска * ? по имени и синониму) +
attr_page/attr_page_size; дефолт — прежний срез 1×50.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def _names(out: dict) -> list[str]:
    return [a["attr_name"] for a in out["attributes"]]


def test_filter_substring_by_name(mini_reader):
    out = tools.object_overview("ОбъектТест", attr_filter="ном")
    assert _names(out) == ["Номенклатура"]     # регистронезависимо
    assert out["attributes_total"] == 1
    assert out["attributes_page"] == {"page": 1, "pages": 1,
                                      "page_size": 50}
    assert out["attributes_kinds"] == {"ts_attribute": 1}
    assert "после фильтра «ном»" in out["attributes_note"]


def test_filter_mask_star_and_question(mini_reader):
    out = tools.object_overview("ОбъектТест", attr_filter="кол*")
    assert _names(out) == ["Количество"]
    out = tools.object_overview("ОбъектТест", attr_filter="к?личество")
    assert _names(out) == ["Количество"]
    out = tools.object_overview("ОбъектТест", attr_filter="*организ*")
    assert _names(out) == ["Организация"]


def test_filter_matches_synonym(mini_reader):
    """«сумма д» — только через синоним «Сумма документа» (имя без
    пробела не содержит подстроку)."""
    out = tools.object_overview("ДокЗаказ", attr_filter="сумма д")
    assert _names(out) == ["СуммаДокумента"]


def test_filter_no_match_is_honest(mini_reader):
    out = tools.object_overview("ОбъектТест", attr_filter="неттакого*")
    assert out["attributes"] == [] and out["attributes_total"] == 0
    assert out["attributes_kinds"] == {}
    # ТЧ — структурная секция, от фильтра не зависит
    assert out["table_sections_total"] == 1


def test_pagination_slices_in_reader_order(mini_reader):
    """Порядок — читательский (ORDER BY attr_kind, attr_name); страница
    2 из 4 при размере 1 — второй реквизит; страница за краем — кламп."""
    out = tools.object_overview("ОбъектТест", attr_page_size=1)
    assert out["attributes_page"]["pages"] == 4
    out2 = tools.object_overview("ОбъектТест", attr_page_size=1,
                                 attr_page=2)
    assert _names(out2) == ["Организация"]
    assert out2["attributes_page"] == {"page": 2, "pages": 4,
                                       "page_size": 1}
    assert out2["attributes_note"].startswith("страница 2 из 4 (по 1)")
    out9 = tools.object_overview("ОбъектТест", attr_page_size=1,
                                 attr_page=99)
    assert out9["attributes_page"]["page"] == 4   # кламп к последней


def test_default_call_is_legacy_slice(mini_reader):
    """Без новых параметров — прежнее поведение: все атрибуты ≤50 без
    note; attributes_page присутствует как честный бейдж пагинации."""
    out = tools.object_overview("ОбъектТест")
    assert len(out["attributes"]) == 4
    assert out["attributes_total"] == 4
    assert "attributes_note" not in out
    assert out["attributes_page"] == {"page": 1, "pages": 1,
                                      "page_size": 50}
    # ТЧ и kinds не сместились
    assert out["attributes_kinds"].get("ts_attribute") == 2
    assert out["table_sections_total"] == 1
