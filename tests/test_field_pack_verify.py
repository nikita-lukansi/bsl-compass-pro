# -*- coding: utf-8 -*-
"""TASK-21: канонайзер имён объектов + трёхуровневый вердикт verify_field."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def test_verify_field_true_attribute(mini_reader):
    out = tools.verify_field("ОбъектТест", "Комментарий")
    assert out["holds"] is True


def test_verify_field_table_section(mini_reader):
    out = tools.verify_field("ОбъектТест", "Товары")
    assert out["holds"] is True and out["kind"] == "table_section"
    assert {c["name"] for c in out["columns"]} == {"Номенклатура", "Количество"}


def test_verify_field_false_with_nearest(mini_reader):
    out = tools.verify_field("ОбъектТест", "Коммент")
    assert out["holds"] is False and out["verdict"] == "field_not_found"
    assert "Комментарий" in out["nearest"]


def test_verify_field_not_indexed(mini_reader):
    # Хозрасчетный: атрибутов нет, но известен по ссылкам
    out = tools.verify_field("ПланСчетов.Хозрасчетный", "ВидыСубконто")
    assert out["holds"] is None and out["verdict"] == "not_indexed"


def test_verify_field_object_not_found(mini_reader):
    out = tools.verify_field("НетТакогоОбъекта", "Поле")
    assert out["holds"] is None and out["verdict"] == "object_not_found"


def test_verify_field_standard_document_attributes(mini_reader):
    """compass-g2r: стандартные реквизиты документа — holds:true с
    evidence {standard: true}, без обращения к индексу (даже регистром
    ниже). Раньше был доказанный false с хинтом «обе секции
    проиндексированы»."""
    for name in ("Номер", "Дата", "ПометкаУдаления", "Проведен", "Ссылка"):
        out = tools.verify_field("Документ.ДокЗаказ", name)
        assert out["holds"] is True, name
        assert out["evidence"]["standard"] is True
        assert out["evidence"]["source"] == "platform"
    assert tools.verify_field("Документ.ДокЗаказ", "нОмЕр")["holds"] is True


def test_verify_field_standard_catalog_attributes(mini_reader):
    """compass-g2r: стандартные реквизиты справочника — holds:true;
    нестандартный реквизит из индекса возвращает индексное evidence."""
    std = tools.verify_field("Справочник.КаталогТест", "Код")
    assert std["holds"] is True and std["evidence"]["standard"] is True
    assert tools.verify_field("Справочник.КаталогТест",
                              "Наименование")["holds"] is True
    indexed = tools.verify_field("Справочник.КаталогТест",
                                 "СлужебныйРеквизит")
    assert indexed["holds"] is True
    assert indexed["evidence"]["attr_name"] == "СлужебныйРеквизит"


def test_verify_field_standard_not_indexed_hint_suppressed(mini_reader):
    """compass-g2r: not_indexed-хинт на стандартные реквизиты не
    выдаётся (ответ true раньше ветки про неиндексированные атрибуты)."""
    out = tools.verify_field("Документ.ДокЗаказ", "Номер")
    assert out["holds"] is True and "hint" not in out


def test_verify_field_standard_negative_keeps_false(mini_reader):
    """compass-g2r: поле вне платформенного списка и вне индекса —
    прежняя логика (field_not_found); чужой вид не подмешивает
    стандартные реквизиты другого вида."""
    bad = tools.verify_field("Документ.ДокЗаказ", "НетТакогоРеквизита")
    assert bad["holds"] is False and bad["verdict"] == "field_not_found"
    wrong = tools.verify_field("ПланСчетов.Хозрасчетный", "Номер")
    assert wrong["holds"] is None and wrong["verdict"] == "not_indexed"


def test_canonical_prefix_stripped(mini_reader):
    # с корректным видом и без — одинаковый результат (compass-1hh: вид
    # в запросе стал фильтром по category, «Справочник.» у объекта с
    # реквизитами CommonModules честно пуст; see v77/1hh)
    assert tools.verify_field("ОбъектТест", "Комментарий") == \
           tools.verify_field("ОбщийМодуль.ОбъектТест", "Комментарий")
    # чужой вид — не канонизируется в «как без вида»: фильтр пуст
    wrong = tools.verify_field("Справочник.ОбъектТест", "Комментарий")
    assert wrong["holds"] is None
