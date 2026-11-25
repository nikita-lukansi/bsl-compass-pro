# -*- coding: utf-8 -*-
"""compass-yho ① (обратка №5): module_hint с сегментом «.Форма.<Имя>»
транслируется в путь Forms выгрузки — read_function/read_module находят
модуль формы («Справочник.X.Форма.Имя» раньше не резолвился).

Раскладка путей сверена с classify.rs vendored gyrfalcon:
Catalogs/Имя/Forms/ФормаСписка/Ext/Form/Module.bsl.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402

# модуль формы в мини-индекс (id 300+ — не пересекается с сидами)
_FORM_MODULE = (
    "Catalogs/КаталогТест/Forms/ФормаЭлемента/Ext/Form/Module.bsl")


@pytest.fixture
def mini_with_form(tmp_path, monkeypatch):
    """Мини-индекс + модуль формы у справочника КаталогТест."""
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.executescript(f"""
     INSERT INTO modules VALUES
      (300, '{_FORM_MODULE}', 'Catalogs', 'КаталогТест', 'Module',
       'ФормаЭлемента', 1, 0, 0);
     INSERT INTO methods VALUES
      (300, 300, 'ПриСозданииНаСервере', 'Процедура', 0, '(Отказ, СтандартнаяОбработка)', 1, 3, 3);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


# ------------------------------------------------------------ трансляция хинта

def test_catalog_form_hint():
    assert tools._module_hint_for_search(
        "Справочник.Номенклатура.Форма.ФормаЭлемента") == \
        "Catalogs/Номенклатура/Forms/ФормаЭлемента"


def test_document_form_hint():
    assert tools._module_hint_for_search(
        "Документ.РеализацияТоваровУслуг.Форма.ФормаДокумента") == \
        "Documents/РеализацияТоваровУслуг/Forms/ФормаДокумента"


def test_form_hint_with_variant():
    """Вариант формы — следующий сегмент пути через слэш."""
    assert tools._module_hint_for_search(
        "Справочник.КаталогТест.Форма.ФормаЭлемента/ВариантУпрощенный") == \
        "Catalogs/КаталогТест/Forms/ФормаЭлемента/ВариантУпрощенный"


def test_case_insensitive_form_segment():
    assert tools._module_hint_for_search(
        "Справочник.X.форма.ФормаЭлемента") == \
        "Catalogs/X/Forms/ФормаЭлемента"


def test_plain_hints_unchanged():
    """Обычные хинты не ломаются: без «.Форма.» — прежняя трансляция."""
    assert tools._module_hint_for_search("Справочник.X") == "Catalogs/X"
    assert tools._module_hint_for_search("Документ.ДокЗаказ") == \
        "Documents/ДокЗаказ"
    assert tools._module_hint_for_search("Catalogs/X/Forms/Форма") == \
        "Catalogs/X/Forms/Форма"       # англ. путь — как есть
    assert tools._module_hint_for_search("") == ""
    assert tools._module_hint_for_search(None) == ""
    # имя объекта, содержащее «Форма» как ЧАСТЬ сегмента, не матчится
    assert tools._module_hint_for_search("Справочник.ФормаДокументов") == \
        "Catalogs/ФормаДокументов"


# ------------------------------------------------------- read_function/module

def test_read_function_finds_form_module(mini_with_form):
    """Форм-хинт находит метод в модуле формы (раньше — «не найден»)."""
    out = tools.read_function(
        "ПриСозданииНаСервере",
        module_hint="Справочник.КаталогТест.Форма.ФормаЭлемента",
        include_body=False)
    assert out["module_path"] == _FORM_MODULE
    assert out["module_type"] == "Module"


def test_read_function_form_hint_not_found_stays_honest(mini_with_form):
    """Хинт на НЕсуществующую форму — честное «не найден» (LIKE пуст)."""
    out = tools.read_function(
        "ПриСозданииНаСервере",
        module_hint="Справочник.КаталогТест.Форма.НетТакойФормы",
        include_body=False)
    assert "error" in out


def test_read_module_form_hint_narrows(mini_with_form):
    """read_module: форм-хинт оставляет только модуль формы (у
    КаталогТест нет других модулей в мини-сиде, поэтому проверяем сам
    rel_path и form_name найденного)."""
    out = tools.read_module(
        "КаталогТест",
        module_hint="Справочник.КаталогТест.Форма.ФормаЭлемента")
    if isinstance(out, list):
        assert len(out) == 1
        out = out[0]
    assert out["rel_path"] == _FORM_MODULE
    assert out["form_name"] == "ФормаЭлемента"
