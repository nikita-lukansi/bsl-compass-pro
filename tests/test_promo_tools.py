# -*- coding: utf-8 -*-
"""Волна 1 / compass-20c: промоушены поверх GyrReader начисто.

code_grep (честное «тела не проиндексированы» на схеме ddl.rs v0.1.20),
call_paths (дерево/путь, циклы, бюджет, depth), who_creates,
check_claims (батч-оракул), index_freshness (mtime-границы).
Мини-фикстура расширена сидами цепочки А→Б→В (mini_index.py), схема не
менялась.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def mini(monkeypatch, tmp_path):
    """Мини-индекс + чистый кэш читателя; реестр/env не задействованы.

    Контекст герметичен: active_config_id → фиктивный cid (иначе тест
    зависел от sessions.json на машине — утечку нашёл S3).
    """
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id",
                        lambda: "Compass_mini_00000000")
    tools.reset_reader()
    yield db
    tools.reset_reader()


# ------------------------------------------------------------------ code_grep

def test_code_grep_honest_no_bodies_for_code_pattern(mini):
    """Схема ddl.rs v0.1.20 (и мини-фикстура): methods_fts = name (+params).
    Тел в FTS нет; запрос с код-символами → честный отказ, не имитация
    поиска по коду (compass-cdf: отказ — только для код-паттернов)."""
    out = tools.code_grep("Посчитать(Партия")
    assert out["bodies_indexed"] is False
    assert "тела методов не проиндексированы" in out["error"]
    assert out["searched_columns"] == ["name", "params"]
    assert "search_function" in out["hint"]


def test_code_grep_name_prefix_searches_names(mini):
    """compass-cdf: именной префиксный запрос на name-only схеме ищет по
    именам (FTS5 prefix query), а не отказывает «тела не проиндексированы»."""
    rows = tools.code_grep("Экспортн*", limit=10)
    assert not isinstance(rows, dict), rows
    names = [x["name"] for x in rows if not x.get("_truncated")]
    assert "ЭкспортнаяФункция" in names
    assert rows[0]["snippet"]                       # сниппет по name
    # без звёздочки — тот же именной маршрут
    plain = tools.code_grep("ЭкспортнаяФункция", limit=10)
    assert not isinstance(plain, dict)
    assert "ЭкспортнаяФункция" in [x["name"] for x in plain
                                   if not x.get("_truncated")]


def test_code_grep_name_multiword_with_prefix(mini):
    """Слова + префикс-звёздочка на токене — тоже именной маршрут."""
    rows = tools.code_grep("ОбработкаП*", limit=10)
    assert not isinstance(rows, dict), rows
    assert "ОбработкаПроведения" in [x["name"] for x in rows
                                     if not x.get("_truncated")]


def test_code_grep_trigram_prefix_real_schema(tmp_path, monkeypatch):
    """Реальная схема ddl.rs v0.1.20: methods_fts(name, trigram).
    Acceptance compass-cdf: code_grep("Скидк*") → результаты по именам,
    не отказ."""
    db = tmp_path / "tri.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE,
            category TEXT, object_name TEXT, module_type TEXT,
            form_name TEXT, is_form INTEGER DEFAULT 0, mtime REAL, size INTEGER);
        CREATE TABLE methods (id INTEGER PRIMARY KEY, module_id INTEGER,
            name TEXT, type TEXT, is_export INTEGER DEFAULT 0, params TEXT,
            line INTEGER, end_line INTEGER, loc INTEGER);
        CREATE VIRTUAL TABLE methods_fts USING fts5(name, tokenize='trigram');
        INSERT INTO modules VALUES (1,
            'CommonModules/Продажи/Ext/Module.bsl', 'CommonModules',
            'Продажи', 'Module', NULL, 0, NULL, NULL);
        INSERT INTO methods VALUES
            (1, 1, 'РассчитатьСкидкуСоглашения', 'Функция', 1, '()', 1, 3, 3),
            (2, 1, 'ПроверитьСкидкиКорзины', 'Функция', 1, '()', 1, 2, 2),
            (3, 1, 'ПровестиДокумент', 'Процедура', 0, '()', 1, 4, 4);
        INSERT INTO methods_fts(rowid, name)
            SELECT id, name FROM methods;
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    try:
        rows = tools.code_grep("Скидк*", limit=5)
        assert not isinstance(rows, dict), rows
        assert {x["name"] for x in rows if not x.get("_truncated")} == {
            "РассчитатьСкидкуСоглашения", "ПроверитьСкидкиКорзины"}
        assert rows[0]["rel_path"].endswith(".bsl")
    finally:
        tools.reset_reader()


def test_code_grep_over_body_column(tmp_path, monkeypatch):
    """Когда в FTS появится тело (гипотетическая схема с колонкой text) —
    тул ищет со сниппетами и честным _truncated при обрезке."""
    db = tmp_path / "grep.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE,
            category TEXT, object_name TEXT, module_type TEXT,
            form_name TEXT, is_form INTEGER DEFAULT 0, mtime REAL, size INTEGER);
        CREATE TABLE methods (id INTEGER PRIMARY KEY, module_id INTEGER,
            name TEXT, type TEXT, is_export INTEGER DEFAULT 0, params TEXT,
            line INTEGER, end_line INTEGER, loc INTEGER);
        CREATE VIRTUAL TABLE methods_fts USING fts5(name, text,
            tokenize='unicode61 remove_diacritics 2');
        INSERT INTO modules VALUES (1, 'CommonModules/Склад/Ext/Module.bsl',
            'CommonModules', 'Склад', 'Module', NULL, 0, NULL, NULL);
        INSERT INTO methods VALUES
            (1, 1, 'ПосчитатьОстатки', 'Функция', 1, '()', 1, 3, 3),
            (2, 1, 'ПровестиПартию', 'Процедура', 0, '()', 1, 4, 4),
            (3, 1, 'ПосчитатьНалоги', 'Функция', 1, '()', 1, 2, 2);
        INSERT INTO methods_fts(rowid, name, text) VALUES
            (1, 'ПосчитатьОстатки',
             'Процедура расчёта: проведённая партия считается по складу'),
            (2, 'ПровестиПартию', 'Партия проводится по складу'),
            (3, 'ПосчитатьНалоги', 'налоги считаются по формуле');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    try:
        rows = tools.code_grep("партия", limit=10)
        assert not isinstance(rows, dict), rows
        assert all(isinstance(x, dict) for x in rows)
        names = [x["name"] for x in rows if not x.get("_truncated")]
        assert set(names) == {"ПосчитатьОстатки", "ПровестиПартию"}
        assert rows[0]["snippet"]                       # сниппет есть
        assert rows[0]["rel_path"].endswith(".bsl")
        cut = tools.code_grep("партия", limit=1)
        assert any(x.get("_truncated") for x in cut)
        bad = tools.code_grep('"незакрытая')
        assert "error" in bad and "FTS5" in bad["error"]
    finally:
        tools.reset_reader()


# ----------------------------------------------------------------- call_paths

def test_call_paths_down_tree_with_cycle(mini):
    out = tools.call_paths("ЦепочкаА")
    assert "error" not in out
    root = out["root"]
    assert root["name"] == "ЦепочкаА"
    children = root["children"]
    assert [c["name"] for c in children] == ["ЦепочкаБ"]
    grandchildren = children[0]["children"]
    names = sorted(c["name"] for c in grandchildren)
    assert names == ["ЦепочкаА", "ЦепочкаВ"]
    cycle = next(c for c in grandchildren if c["name"] == "ЦепочкаА")
    assert cycle.get("cycle") is True            # цикл А→Б→А помечен
    v = next(c for c in grandchildren if c["name"] == "ЦепочкаВ")
    leaves = v["children"]
    assert all(leaf.get("unresolved") for leaf in leaves)
    assert {leaf["name"] for leaf in leaves} == {
        "Документы.ДокЗаказ.СоздатьДокумент", "ПровестиЛогику"}
    assert out["nodes_total"] >= 5


def test_call_paths_up_direction(mini):
    out = tools.call_paths("ЦепочкаВ", direction="up")
    root = out["root"]
    assert root["name"] == "ЦепочкаВ"
    child = root["children"][0]
    assert child["name"] == "ЦепочкаБ"
    assert child["children"][0]["name"] == "ЦепочкаА"
    # поднимаясь выше, упираемся в посещённое — цикл защищён
    up_cycle = child["children"][0]["children"][0]
    assert up_cycle["name"] == "ЦепочкаБ" and up_cycle.get("cycle") is True


def test_call_paths_depth_limit(mini):
    out = tools.call_paths("ЦепочкаА", depth=1)
    assert out["depth_reached"] == 1
    assert "children" not in out["root"]["children"][0]  # без внуков


def test_call_paths_truncated_by_node_budget(mini, monkeypatch):
    monkeypatch.setattr(tools, "CALL_PATHS_MAX_NODES", 3)
    out = tools.call_paths("ЦепочкаА")
    assert out.get("_truncated") is True
    assert out["nodes_total"] <= 3
    assert "бюджет" in out["hint"]


def test_call_paths_ambiguous_start(mini):
    """Два перехватчика с одним именем (Расш1/Расш2) — отказ с кандидатами."""
    out = tools.call_paths("ЗаполнитьКонтактнуюИнформацию_Перед")
    assert "error" in out and "неоднозначно" in out["error"]
    assert len(out["candidates"]) == 2


def test_call_paths_errors(mini):
    assert "не найден" in tools.call_paths("НетТакого")["error"]
    assert "direction" in tools.call_paths("ЦепочкаА", direction="sideways")["error"]


def test_call_paths_target_found(mini):
    out = tools.call_paths("ЦепочкаА", target="ЦепочкаВ")
    assert out["found"] is True
    names = [p["name"] for p in out["path"]]
    assert names == ["ЦепочкаА", "ЦепочкаБ", "ЦепочкаВ"]  # кратчайший (BFS)


def test_call_paths_target_unresolved_name(mini):
    """Цель — неразрешённое имя вызова (не метод индекса) — путь найден
    с честной пометкой про последний шаг."""
    out = tools.call_paths("ЦепочкаА", target="ПровестиЛогику")
    assert out["found"] is True
    assert out["path"][-1]["name"] == "ПровестиЛогику"
    assert "не разрешён" in out["note"]


def test_call_paths_target_not_found(mini):
    out = tools.call_paths("ЦепочкаА", target="НетТакогоНигдеХХХ")
    assert out["found"] is False and "depth" in out["hint"]


# ---------------------------------------------------------------- who_creates

def test_who_creates_by_calls_usages_and_refs(mini):
    out = tools.who_creates("Документ.ДокЗаказ")       # канонизация имени
    assert out["object"] == "ДокЗаказ"
    assert out["creators_total"] == 1
    creator = out["creators"][0]
    assert creator["caller"] == "ЦепочкаВ"
    assert creator["callee_name"] == "Документы.ДокЗаказ.СоздатьДокумент"
    assert out["create_usages_total"] >= 1
    assert any(u["member_path"] == "Документы.ДокЗаказ.Создать"
               for u in out["create_usages"])
    assert out["referenced_by_total"] >= 1             # ссылки из метаданных


def test_who_creates_no_matches(mini):
    out = tools.who_creates("ОбъектТест")
    assert out.get("_no_matches") is True and out["object"] == "ОбъектТест"


# ---------------------------------------------------------------- check_claims

def test_check_claims_batch_mixed(mini):
    claims = [
        {"kind": "field", "object": "ОбъектТест", "field": "Комментарий"},
        {"kind": "field", "object": "ОбъектТест", "field": "Товары"},
        {"kind": "field", "object": "ОбъектТест", "field": "НетТакогоПоля"},
        {"kind": "call", "caller": "ОбработкаПроведения",
         "callee": "ЭкспортнаяФункция"},
        {"kind": "call", "caller": "ЦепочкаВ", "callee": "ЭкспортнаяФункция"},
        {"kind": "banan", "object": "X"},
        {"kind": "field", "object": "ОбъектТест"},
        "не словарь",
    ]
    out = tools.check_claims(claims)
    assert out["total"] == 8
    res = out["results"]
    assert res[0]["holds"] is True                     # шапочный реквизит
    assert res[1]["holds"] is True and res[1]["kind"] == "table_section"
    assert res[2]["holds"] is False                    # field_not_found
    assert res[3]["holds"] is True and res[3]["evidence"]
    assert res[4]["holds"] is False
    assert res[5]["holds"] is None and res[5]["verdict"] == "invalid_claim"
    assert res[6]["holds"] is None and res[6]["verdict"] == "invalid_claim"
    assert res[7]["holds"] is None and res[7]["verdict"] == "invalid_claim"
    assert out["holds_true"] == 3
    assert out["holds_false"] == 2
    assert out["holds_unknown"] == 3


def test_check_claims_bad_input(mini):
    assert "error" in tools.check_claims([])
    assert "error" in tools.check_claims("не список")  # type: ignore[arg-type]


# ------------------------------------------------------------- index_freshness

def test_index_freshness_no_index(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "active_index_db", lambda: None)
    tools.reset_reader()
    try:
        out = tools.index_freshness()
        assert "error" in out
    finally:
        tools.reset_reader()


def test_index_freshness_no_registry_entry(mini):
    """Читатель есть, записи реестра нет (голая мини-фикстура) — честное
    «сборка не зафиксирована»."""
    out = tools.index_freshness()
    assert out["status"] == "unknown"
    assert "indexed_at" in out["verdict"]


def test_index_freshness_stale_and_fresh(mini, monkeypatch):
    db = tools.active_index_db()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE modules SET mtime = 2000000000")
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.registry.get_config",
                        lambda cid: {"indexed_at":
                                     "2026-01-01T00:00:00+00:00"})
    stale = tools.index_freshness()
    assert stale["status"] == "stale"
    assert stale["lag_seconds"] == 2000000000 - 1767225600
    assert "исходник новее индекса" in stale["verdict"]

    conn = sqlite3.connect(db)
    conn.execute("UPDATE modules SET mtime = 1000")
    conn.commit()
    conn.close()
    fresh = tools.index_freshness()
    assert fresh["status"] == "fresh" and fresh["verdict"] == "свежо"


def test_index_freshness_null_mtime_unknown(mini, monkeypatch):
    """ddl.rs v0.1.20: строитель mtime не пишет — NULL → честное «не
    определим» с hint на built_at/rebuild_incremental."""
    db = tools.active_index_db()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE modules SET mtime = NULL")
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.registry.get_config",
                        lambda cid: {"indexed_at":
                                     "2026-01-01T00:00:00+00:00"})
    out = tools.index_freshness()
    assert out["status"] == "unknown"
    assert "не заполняется строителем" in out["verdict"]
    assert "rebuild_incremental" in out["hint"]


# ----------------------------------------------------------- reader-уровень

def test_resolve_callee_exact_and_qualified(mini):
    r = tools.reader()
    exact = r.resolve_callee("ЦепочкаБ")
    assert [m["id"] for m in exact] == [4]
    qualified = r.resolve_callee("ТестМодуль.ЭкспортнаяФункция")
    assert qualified and qualified[0]["id"] == 1
    assert r.method_by_id(5)["name"] == "ЦепочкаВ"
    assert r.method_by_id(10 ** 9) is None
    assert r.fts_columns() == ["name", "params"]
