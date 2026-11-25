# -*- coding: utf-8 -*-
"""compass-4w7.1 (обратная связь 2026-11-15): синтаксис «ТЧ.Колонка».

Репро: запрос колонки ТЧ с точкой не матчился ни шагом 1 (голое имя),
ни шагом 2 (имя ТЧ целиком) и падал до шага 3 → ложный field_not_found
при живой колонке. Шаг 1b парсит «ТЧ.Колонка»; ветка v77 not_indexed
(объекты без индексированных секций ТЧ) не тронута.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def doc_reader(tmp_path, monkeypatch):
    """Мини-индекс + документ-образец: у ОбъектТест секция ТЧ «Товары»
    дополнена колонками Сумма, СуммаНДС, НомерГТД (стиль mini_reader)."""
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.executescript("""
        INSERT INTO object_attributes (id, object_name, category, attr_name,
            attr_synonym, attr_type, attr_kind, ts_name, source_file,
            length, precision) VALUES
         (20, 'ОбъектТест', 'CommonModules', 'Сумма', 'Сумма',
          '["Number"]', 'ts_attribute', 'Товары', 'f.xml', 15, 2),
         (21, 'ОбъектТест', 'CommonModules', 'СуммаНДС', 'Сумма НДС',
          '["Number"]', 'ts_attribute', 'Товары', 'f.xml', 15, 2),
         (22, 'ОбъектТест', 'CommonModules', 'НомерГТД', 'Номер ГТД',
          '["String"]', 'ts_attribute', 'Товары', 'f.xml', 0, 0);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


def test_ts_column_syntax_found(doc_reader):
    """Репро обратки: «Товары.Сумма» → holds True, evidence из секции ТЧ."""
    out = tools.verify_field("ОбъектТест", "Товары.Сумма")
    assert out["holds"] is True
    assert out["evidence"]["ts_name"] == "Товары"
    assert out["evidence"]["attr_name"] == "Сумма"


def test_bare_column_still_found(doc_reader):
    """Регрессия шага 1: голая колонка ТЧ находится по-прежнему."""
    out = tools.verify_field("ОбъектТест", "СуммаНДС")
    assert out["holds"] is True


def test_ts_column_missing_nearest(doc_reader):
    """«Товары.НеКолонка» → False, nearest — колонки секции Товары."""
    out = tools.verify_field("ОбъектТест", "Товары.НеКолонка")
    assert out["holds"] is False and out["verdict"] == "field_not_found"
    assert {"Сумма", "СуммаНДС", "НомерГТД"} <= set(out["nearest"])


def test_unknown_section(doc_reader):
    """«НеТЧ.Колонка» при индексированных секциях → False «нет ТЧ»."""
    out = tools.verify_field("ОбъектТест", "НеТЧ.Колонка")
    assert out["holds"] is False and out["verdict"] == "field_not_found"
    assert "НеТЧ" in out["hint"]


def test_not_indexed_branch_intact(doc_reader):
    """v77 цел: у плана счетов секций ТЧ в индексе нет — запрос «ТЧ.Колонка»
    обязан провалиться к честному not_indexed, а не дать False."""
    out = tools.verify_field("ПланСчетов.Хозрасчетный", "ВидыСубконто.Субконто1")
    assert out["holds"] is None and out["verdict"] == "not_indexed"


def test_table_section_regression(doc_reader):
    """Регрессия шага 2: голое имя ТЧ → True kind=table_section."""
    out = tools.verify_field("ОбъектТест", "Товары")
    assert out["holds"] is True and out["kind"] == "table_section"


def test_header_attribute_regression(doc_reader):
    """Регрессия шага 1: реквизит шапки — True."""
    out = tools.verify_field("ОбъектТест", "Комментарий")
    assert out["holds"] is True


def test_check_claims_ts_syntax(doc_reader):
    """check_claims реюзнет verify_field — батч с «Товары.Сумма» держит."""
    out = tools.check_claims(
        [{"kind": "field", "object": "ОбъектТест", "field": "Товары.Сумма"}])
    assert out["results"][0]["holds"] is True
    assert out["holds_true"] == 1
