# -*- coding: utf-8 -*-
"""Тесты тулов фасада — герметичные, на мини-индексе (A12/P2).

Мини-индекс (tests/mini_index.py) сидируется под кейсы каждого тула;
реальный реестр/прод-индекс не читается (до A12 reader открывал
постороннюю базу — sqlite3.OperationalError на 13 тестах). Один
интеграционный кейс СКД остаётся на живом дампе с честным skipif.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# С7 (аудит публикации): путей владельца в дефолтах нет — только env
# COMPASS_SOURCE_ROOT; без env живые тесты честно скипаются.
_ROOT = (Path(os.environ["COMPASS_SOURCE_ROOT"])
         if os.environ.get("COMPASS_SOURCE_ROOT") else None)

from src.facade import tools  # noqa: E402
from tests.mini_index import make_source_tree  # noqa: E402


@pytest.fixture
def mini(monkeypatch, tmp_path):
    """Мини-индекс + чистый кэш читателя; реестр/env не задействованы."""
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


def test_search_function_exact_and_hint(mini):
    rows = tools.search_function("ОбработкаПроведения", limit=5)
    assert isinstance(rows, list) and rows[0]["name"] == "ОбработкаПроведения"
    empty = tools.search_function("ТакогоНетВовсемиреХХХ")
    assert empty[0].get("_no_matches") is True and "hint" in empty[0]


def test_read_function_with_body_and_graph(monkeypatch, mini):
    monkeypatch.setenv("COMPASS_SOURCE_ROOT",
                       str(make_source_tree(mini.parent)))
    out = tools.read_function("ОбработкаПроведения", include_body=True)
    assert out["module_path"].endswith(".bsl")
    assert out["ambiguous_total"] >= 1
    assert "body" in out and len(out["body"]) > 20
    assert out["callees"] and "callers" in out


def test_triggers_on_write_full_picture(mini):
    out = tools.triggers_on_write("ДокЗаказ")
    handlers = {h["name"] for h in out["handlers"]}
    assert "ОбработкаПроведения" in handlers
    assert out["movements"], "у ДокЗаказа есть заявленные движения"
    assert isinstance(out["subscriptions"], list)


def test_object_overview_composite(mini):
    out = tools.object_overview("ДокЗаказ")
    assert out["attributes_total"] >= 2
    assert out["subsystems"] and out["top_methods"] and out["forms"]
    # в фикстуре ДокЗаказ сам ссылается на Хозрасчетный (ref_object),
    # обратных ссылок на него нет — references проверяются ниже по reader'у
    assert "referenced_by_total" in out


def test_who_touches_and_references(mini):
    wt = tools.who_touches("ДокЗаказ")
    assert wt["code_usages"]
    # имя регистра берём из движений самого документа (не хардкод)
    mov = tools.triggers_on_write("ДокЗаказ")["movements"]
    reg_name = mov[0]["register_name"]
    reg = tools.who_touches(reg_name)
    assert reg.get("registrars"), \
        f"у регистра {reg_name} должны быть регистраторы"
    # references_to: на Хозрасчетный ссылаются реквизиты ДокЗаказа (2 строки)
    refs = tools.reader().references_to("Хозрасчетный")
    assert len(refs) == 2 and refs[0]["source_object"] == "ДокЗаказ"


def test_verify_field_oracles(mini):
    ok = tools.verify_field("ДокЗаказ", "Контрагент")
    assert ok["holds"] is True and ok["evidence"]["attr_name"] == "Контрагент"
    bad = tools.verify_field("ДокЗаказ", "НетТакогоРеквизита")
    assert bad["holds"] is False and isinstance(bad["nearest"], list)


def test_verify_call_oracle(mini):
    # ХОРОШИЙ_ПРОВЕРЯЕМЫЙ_ВЫЗОВ подбирём динамически: первый callee метода
    r = tools.reader()
    callee = r.callees(r.get_method("ОбработкаПроведения")[0]["id"],
                       only_resolved=True)[0]["callee_name"]
    callee_short = callee.split(".")[-1]
    verdict = tools.verify_call("ОбработкаПроведения", callee_short)
    assert verdict["holds"] is True and verdict["evidence"]


def test_verify_call_qualified_names(mini):
    """compass-m3z: квалифицированные имена (2+ точки) не дают ложного
    holds:false — ребро, которое строит call_paths, подтверждается.
    В сиде: ОбработкаПроведения → callee_name «ТестМодуль.ЭкспортнаяФункция»."""
    verdict = tools.verify_call(
        "Документ.ДокЗаказ.ОбработкаПроведения",
        "ОбщийМодуль.ТестМодуль.ЭкспортнаяФункция")
    assert verdict["holds"] is True and verdict["evidence"]
    # голые имена работают как раньше (последний сегмент = то же имя)
    bare = tools.verify_call("ОбработкаПроведения", "ЭкспортнаяФункция")
    assert bare["holds"] is True


def test_verify_call_negative_keeps_false(mini):
    """compass-m3z: отрицательный вердикт жив — несуществующего ребра нет."""
    verdict = tools.verify_call("ОбработкаПроведения",
                                "ТакогоМетодаНеСуществуетХХХ")
    assert verdict["holds"] is False and "hint" in verdict
    # квалифицированное имя того же несуществующего метода — тоже false
    qualified = tools.verify_call("ОбщийМодуль.ТестМодуль.ОбработкаПроведения",
                                  "Модуль.ТакогоМетодаНеСуществуетХХХ")
    assert qualified["holds"] is False


def test_check_claims_call_qualified(mini):
    """compass-m3z: батч-оракул повторяет вердикты verify_call на
    квалифицированных именах (та же SQL-логика по ТЗ)."""
    out = tools.check_claims([
        {"kind": "call",
         "caller": "Документ.ДокЗаказ.ОбработкаПроведения",
         "callee": "ОбщийМодуль.ТестМодуль.ЭкспортнаяФункция"},
        {"kind": "call", "caller": "ОбработкаПроведения",
         "callee": "ТакогоМетодаНеСуществуетХХХ"},
    ])
    assert out["results"][0]["holds"] is True
    assert out["results"][1]["holds"] is False


def test_subsystems_overview_and_of(mini):
    overview = tools.subsystems_overview()
    assert overview and overview[0]["objects"] >= 1
    subs = tools.subsystems_of("ДокЗаказ")
    assert subs and "subsystem_name" in subs[0]


def test_no_index_marker(monkeypatch):
    """Индекс не настроен — честный error, не пустота и не падение."""
    monkeypatch.setattr(tools, "active_index_db", lambda: None)
    monkeypatch.delenv("COMPASS_INDEX_DB", raising=False)
    tools._reader = None
    res = tools.search_function("x")
    assert isinstance(res, dict) and "error" in res
    tools._reader = None  # вернуть честное состояние для остальных тестов


@pytest.mark.skipif(_ROOT is None or not _ROOT.exists(),
                    reason="живой дамп недоступен: задайте COMPASS_SOURCE_ROOT")
def test_get_skd_info_real_template():
    """Находим реальный СКД-макет в дампе и разбираем его."""
    # ищем первый макет с НАБОРОМ-ЗАПРОСОМ (наборы-объекты query не имеют)
    skd = None
    for c in _ROOT.rglob("Ext/Template.xml"):
        head = c.read_bytes()[:400]
        if b"DataComposition" not in head:
            continue
        info = tools.get_skd_info(str(c))
        if isinstance(info, dict) and any(
                ds.get("kind") == "query" and ds.get("query")
                for ds in info.get("data_sets", [])):
            skd = c
            break
    if skd is None:
        # фолбэк: любой СКД-макет (набор-объект тоже валиден)
        for c in _ROOT.rglob("Ext/Template.xml"):
            if b"DataComposition" in c.read_bytes()[:2000]:
                skd = c
                break
    if skd is None:
        pytest.skip("СКД-макеты в дампе не нашлись")
    info = tools.get_skd_info(str(skd))
    assert info.get("data_sets"), "у СКД должен быть набор данных"
    assert all("kind" in ds for ds in info["data_sets"])


def test_read_module_structure_and_body(monkeypatch, mini):
    """Волна 1 п.2: read_module — заголовок, области, методы; тело по
    запросу для одного модуля."""
    out = tools.read_module("ОбъектТест")
    assert out["rel_path"].endswith("Module.bsl")
    assert out["header_comment"].startswith("Тестовый")
    assert [r["name"] for r in out["regions"]] == \
        ["ПрограммныйИнтерфейс", "СлужебныеПроцедуры"]
    assert out["methods"][0]["name"] == "ЭкспортнаяФункция"

    # тело по запросу (нужен COMPASS_SOURCE_ROOT)
    monkeypatch.setenv("COMPASS_SOURCE_ROOT",
                       str(make_source_tree(mini.parent)))
    tools._reader = None
    out2 = tools.read_module("ОбъектТест", include_body=True)
    assert "body" in out2 and out2["body_lines"] == [1, 5]

    # несколько модулей (Контакты из расширений) — без module_hint список,
    # тело честно не читается
    multi = tools.read_module("Контакты", include_body=True)
    assert isinstance(multi, list) and len(multi) == 3
    assert all("body_info" in m for m in multi)
    # module_hint сужает до одного
    one = tools.read_module("Контакты", module_hint="Расш2", include_body=True)
    assert isinstance(one, dict) and "расш2" in one["rel_path"].lower()


def test_list_objects_filters(monkeypatch, mini):
    """Волна 1 п.3: list_objects — поиск (FTS trigram), категория,
    подсистема; честная пустота."""
    rows = tools.list_objects(search="Заказ")
    assert len(rows) == 1 and rows[0]["object_name"] == "ДокЗаказ"
    assert rows[0]["synonym"] == "Заказ клиента"

    rows = tools.list_objects(category="AccumulationRegisters")
    assert len(rows) == 1 and rows[0]["object_name"] == "ТоварыНаСкладах"

    rows = tools.list_objects(subsystem="Продажи")
    assert len(rows) == 1 and rows[0]["object_name"] == "ДокЗаказ"

    rows = tools.list_objects()
    assert {r["object_name"] for r in rows} == \
        {"ОбъектТест", "ДокЗаказ", "ТоварыНаСкладах"}

    empty = tools.list_objects(search="НесуществующийХХХ")
    assert empty[0]["_no_matches"] is True


def test_object_overview_enum_values(mini):
    """F3/1v3: object_overview перечисления отдаёт enum_values (имена —
    синонимы значений мета не сохраняет); пустое — честная пометка."""
    out = tools.object_overview("ХозяйственныеОперации")
    ev = out["enum_values"]
    assert ev["name"] == "ХозяйственныеОперации"
    assert ev["total"] == 3 and ev["values"][0] == "ПоступлениеДопРасходов"
    assert ev["note"] == ""

    empty = tools.object_overview("ПустоеПеречисление")
    assert empty["enum_values"]["values"] == []
    assert empty["enum_values"]["note"] == "перечисление без значений"

    # не-перечисление — ключа нет
    assert "enum_values" not in tools.object_overview("ДокЗаказ")


def test_module_hint_russian_prefix(mini):
    """F4/kwn: read_function с module_hint «Документ.ДокЗаказ» находит
    метод (транслируется в Documents/ДокЗаказ); англ. хинт и без хинта
    работают как раньше; who_overrides — та же трансляция."""
    out = tools.read_function("ОбработкаПроведения",
                              module_hint="Документ.ДокЗаказ")
    assert out["module_path"].endswith("ManagerModule.bsl")
    out = tools.read_function("ОбработкаПроведения",
                              module_hint="Documents/ДокЗаказ")
    assert out["module_path"].endswith("ManagerModule.bsl")
    out = tools.read_function("ОбработкаПроведения")
    assert out["module_path"].endswith("ManagerModule.bsl")
    out = tools.read_function("ЭкспортнаяФункция",
                              module_hint="Справочник.НетТакогоКаталога")
    assert "error" in out  # хинт сузил до несуществующего каталога


def test_callers_qualified_callee(mini):
    """F5/406: callers находит и квалифицированный callee «Модуль.Имя»
    (суффикс предвычислен параметром, не конкатенацией в скане)."""
    r = tools.reader()
    rows = r.callers("ЭкспортнаяФункция")
    assert any(row["caller_name"] == "ОбработкаПроведения" for row in rows)
    verdict = tools.verify_call("ОбработкаПроведения", "ЭкспортнаяФункция")
    assert verdict["holds"] is True


def test_no_index_distinct_branches(mini, monkeypatch):
    """F6/9n8: 4 ветки _no_index возвращают отличимые подсказки."""
    # явные ветки
    assert "не выбрана" in tools._no_index("no_config")["error"]
    assert "config_use" in tools._no_index("no_config")["error"]
    assert "не собран" in tools._no_index("not_built")["error"]
    assert "/reindex" in tools._no_index("not_built")["error"]

    # env-ветка (dev): env упомянут только здесь
    monkeypatch.setenv("COMPASS_INDEX_DB", "D:/нет/такого/index.db")
    env_err = tools._no_index()["error"]
    assert "COMPASS_INDEX_DB=D:/нет/такого/index.db" in env_err
    assert "несуществующий файл" in env_err

    # автодетект: активной конфигурации нет → no_config
    monkeypatch.delenv("COMPASS_INDEX_DB", raising=False)
    monkeypatch.setattr(tools, "active_index_db", lambda: None)
    import src.context as ctx
    monkeypatch.setattr(ctx, "active_config_id", lambda: None)
    tools._reader = None
    auto = tools._no_index()["error"]
    assert "не выбрана" in auto
    tools._reader = None


def test_no_index_building_branch(mini, tmp_path, monkeypatch):
    """compass-epw: на первой сборке файла индекса ещё нет — тулам честный
    статус building, а не not_built (BUILD_STATE.building)."""
    import src.context as ctx
    monkeypatch.delenv("COMPASS_INDEX_DB", raising=False)
    monkeypatch.setattr(tools, "active_index_db", lambda: None)
    monkeypatch.setattr(ctx, "active_config_id", lambda: "cid-1")
    from src.state import BUILD_STATE
    monkeypatch.setattr(
        "src.registry.get_config",
        lambda cid: {"id": cid, "index_db": str(tmp_path / "index.db")})
    # сборка не идёт → not_built
    monkeypatch.setattr("src.state.BUILD_STATE", {**BUILD_STATE, "building": False})
    tools._reader = None
    assert "не собран" in tools._no_index()["error"]
    # идёт сборка → building
    monkeypatch.setattr("src.state.BUILD_STATE", {**BUILD_STATE, "building": True})
    tools._reader = None
    err = tools._no_index()["error"]
    assert "идёт сборка" in err and "повторите" in err
    tools._reader = None
