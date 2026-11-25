# -*- coding: utf-8 -*-
"""TASK-22: predefined_details/account_subaccounts — generic-парсер
Predefined.xml выгрузки + субконто предопределённых счетов."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


@pytest.fixture
def dump(tmp_path, monkeypatch):
    """Мини-выгрузка: план счетов с предопределённым счётом 41 и ТЧ
    субконто (имя узла «ВидыСубконто» — generic-парсер переживёт любое)."""
    base = tmp_path / "ChartsOfAccounts" / "Хозрасчетный" / "Ext"
    base.mkdir(parents=True)
    (base / "Predefined.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<PredefinedData xmlns="http://v8.1c.ru/8.3/xcf/predef"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:type="ChartOfAccountsPredefinedItems" version="2.20">
  <Item id="a1"><Name>Товары</Name><Code>41</Code><Description>Товары</Description>
    <IsFolder>false</IsFolder><Order>1</Order>
    <ВидыСубконто>
      <Row><ВидСубконто>Номенклатура</ВидСубконто><ВидСубконтоХозрасчетный>Склады</ВидСубконтоХозрасчетный></Row>
      <Row><ВидСубконто>Партнеры</ВидСубконто></Row>
    </ВидыСубконто>
  </Item>
  <Item id="a2"><Name>Расчеты</Name><Code>60</Code><Description/><IsFolder>false</IsFolder>
    <ВидыСубконто><Row><ВидСубконто>Контрагенты</ВидСубконто></Row></ВидыСубконто>
  </Item>
</PredefinedData>""", encoding="utf-8")
    monkeypatch.setenv("COMPASS_SOURCE_ROOT", str(tmp_path))
    return tmp_path


def test_parse_predefined_generic(dump):
    from src.parsers.predefined import parse_predefined
    items = parse_predefined(dump / "ChartsOfAccounts" / "Хозрасчетный"
                             / "Ext" / "Predefined.xml")
    assert items is not None and len(items) == 2
    first = items[0]
    assert first["code"] == "41" and first["extra"].get("Order") == "1"
    assert len(first["tables"]["ВидыСубконто"]) == 2


def test_account_subaccounts_by_code(dump):
    out = tools.account_subaccounts("Хозрасчетный", "41")
    assert out["code"] == "41"
    assert {r["ВидСубконто"] for r in out["subaccounts"]} == \
           {"Номенклатура", "Партнеры"}


def test_account_subaccounts_by_name(dump):
    out = tools.account_subaccounts("ПланСчетов.Хозрасчетный", "Расчеты")
    assert out["code"] == "60" and out["subaccounts"][0]["ВидСубконто"] == "Контрагенты"


def test_predefined_details_not_found(dump, monkeypatch):
    out = tools.predefined_details("НетТакогоОбъекта")
    assert "error" in out and "tried" in out
