# -*- coding: utf-8 -*-
"""compass-yho ② (обратка №5): object_overview — секция owners (владельцы
справочника) и standard_attributes (стандартные реквизиты вида) из XML
выгрузки: индекс их не хранит, vendored-схема не расширяется.

Структура XML сверена с живой выгрузкой КА 2.5:
Catalogs/<Имя>.xml → MetaDataObject/Catalog/Properties/
{Owners/Item, StandardAttributes/xr:StandardAttribute[@name]}.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402

_CATALOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
 xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
 <Catalog>
  <Properties>
   <Name>КаталогТест</Name>
   <Owners>
    <Item>Catalog.ДоговорыКонтрагентов</Item>
    <Item>Document.ЗаказКлиента</Item>
   </Owners>
   <StandardAttributes>
    <xr:StandardAttribute name="Parent"/>
    <xr:StandardAttribute name="Code"/>
    <xr:StandardAttribute name="Description"/>
    <xr:StandardAttribute name="СлужебныйРеквизит"/>
   </StandardAttributes>
  </Properties>
 </Catalog>
</MetaDataObject>
"""

_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
 xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
 <Document>
  <Properties>
   <Name>ДокЗаказ</Name>
   <StandardAttributes>
    <xr:StandardAttribute name="Number"/>
    <xr:StandardAttribute name="Date"/>
   </StandardAttributes>
  </Properties>
 </Document>
</MetaDataObject>
"""


@pytest.fixture
def overview_env(tmp_path, monkeypatch):
    """Мини-индекс + дом source с XML объектов; source_root через env."""
    db = make_mini_db(tmp_path)
    src = tmp_path / "source_root"
    (src / "Catalogs").mkdir(parents=True)
    (src / "Documents").mkdir(parents=True)
    (src / "Catalogs" / "КаталогТест.xml").write_text(
        _CATALOG_XML, encoding="utf-8")
    (src / "Documents" / "ДокЗаказ.xml").write_text(
        _DOCUMENT_XML, encoding="utf-8")
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    monkeypatch.setenv("COMPASS_SOURCE_ROOT", str(src))
    tools.reset_reader()
    yield src
    tools.reset_reader()


def test_catalog_owners_section(overview_env):
    """Владельцы справочника — полные имена из Owners/Item."""
    out = tools.object_overview("КаталогТест")
    assert out["owners"] == ["Catalog.ДоговорыКонтрагентов",
                             "Document.ЗаказКлиента"]


def test_standard_attributes_from_xml(overview_env):
    """Стандартные реквизиты вида — имена xr:StandardAttribute/@name,
    отсутствующие среди проиндексированных атрибутов (СлужебныйРеквизит
    в индексе есть — не дублируется)."""
    out = tools.object_overview("КаталогТест")
    assert out["standard_attributes"] == ["Parent", "Code", "Description"]
    assert "СлужебныйРеквизит" not in out["standard_attributes"]


def test_owners_only_for_catalogs(overview_env):
    """Документ: standard_attributes есть, owners — нет (секция
    справочная, у не-справочников владельцев не бывает)."""
    out = tools.object_overview("ДокЗаказ")
    assert out["standard_attributes"] == ["Number", "Date"]
    assert "owners" not in out


def test_honest_absence_without_xml(tmp_path, monkeypatch):
    """Нет source_root — нет ни owners, ни standard_attributes (честное
    отсутствие: ни индекса-фактов, ни XML)."""
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    monkeypatch.delenv("COMPASS_SOURCE_ROOT", raising=False)
    tools.reset_reader()
    try:
        out = tools.object_overview("КаталогТест")
        assert "owners" not in out
        assert "standard_attributes" not in out
    finally:
        tools.reset_reader()


def test_broken_xml_is_honest_absence(overview_env, tmp_path):
    """Битый XML → секций нет, обзор не падает."""
    (overview_env / "Catalogs" / "КаталогТест.xml").write_text(
        "<MetaDataObject><Catalog>", encoding="utf-8")
    out = tools.object_overview("КаталогТест")
    assert "owners" not in out
    assert "standard_attributes" not in out
    assert out["object"] == "КаталогТест"   # сам обзор жив
