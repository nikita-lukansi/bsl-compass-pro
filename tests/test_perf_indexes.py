# -*- coding: utf-8 -*-
"""Гейты производительности SQL (аудит PERF-2/P36, compass-44g).

Семантика callers на рёбрах с 2+ точками (запрет short-семантики по первой
точке), планы запросов (нет SCAN calls/methods — ходим по индексам, включая
суффиксный idx_calls_callee_last) и объём выборки repomap (узлы ранга,
не вся таблица methods).
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.recon.gyr_reader import GyrReader  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402

# полные переборы таблиц данных (алиасы как в реальных запросах reader'а)
_SCAN_OF_DATA = re.compile(r"\bSCAN (c|calls|m|m2|mo|methods)\b")


@pytest.fixture
def mini_reader(tmp_path):
    """Мини-фикстура + пара квалифицированных рёбер с 1 и 2 точками."""
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    # ребро с 2+ точками терялось бы на idx_calls_callee_short (срез по
    # ПЕРВОЙ точке) — семантика «последней точки» обязана его ловить
    conn.executescript("""
        INSERT INTO calls VALUES
         (20, 3, 'ОбъектТест.ТестМодуль.ЭкспортнаяФункция', 5, NULL,
          'resolved', 1.0),
         (21, 4, 'ТестМодуль.ЭкспортнаяФункция', 6, NULL, 'resolved', 1.0);
    """)
    conn.commit()
    conn.close()
    reader = GyrReader(db)
    yield reader
    reader.close()


def _plan_db(tmp_path: Path) -> Path:
    """Индекс, разогретый по объёму (2000 методов/calls): на карликовых
    данных оптимизатор SQLite вправе выбрать SCAN, планы проверяем там,
    где индекс заведомо дешевле скана. Схема = зеркало ddl.rs (компас-44g):
    только таблицы под проверяемые запросы + их индексы."""
    db = tmp_path / "plan_index.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE modules (id INTEGER PRIMARY KEY,
            rel_path TEXT UNIQUE NOT NULL, object_name TEXT, module_type TEXT);
        CREATE TABLE methods (id INTEGER PRIMARY KEY,
            module_id INTEGER NOT NULL REFERENCES modules(id),
            name TEXT NOT NULL, is_export INTEGER DEFAULT 0, params TEXT);
        CREATE TABLE calls (id INTEGER PRIMARY KEY,
            caller_id INTEGER NOT NULL REFERENCES methods(id),
            callee_name TEXT NOT NULL, line INTEGER,
            resolution TEXT NOT NULL, confidence REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(
            callee_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_calls_callee_last ON calls(
            (substr(callee_name, length(rtrim(callee_name,
             replace(callee_name, '.', ''))) + 1)) COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_meth_name ON methods(
            name COLLATE NOCASE);
    """)
    conn.execute(
        "INSERT INTO modules VALUES (1, 'a/Объект/Module.bsl', 'Объект', 'Module')")
    conn.execute(
        "INSERT INTO modules VALUES (2, 'b/Другой/Module.bsl', 'Другой', 'Module')")
    conn.executemany("INSERT INTO methods VALUES (?, 1, ?, 1, '()')",
                     ((i, f"Метод{i}") for i in range(1, 6)))
    conn.executemany("INSERT INTO methods VALUES (?, 2, ?, 0, '()')",
                     ((i, f"Чужой{i}") for i in range(6, 2001)))
    conn.executemany(
        "INSERT INTO calls VALUES (?, ?, ?, ?, 'resolved', 1.0)",
        ((i, (i % 1995) + 6, f"Объект.Модуль.Метод{(i % 5) + 1}", i)
         for i in range(1, 2001)))
    conn.commit()
    conn.close()
    return db


def _capture_queries(monkeypatch):
    """Spy на GyrReader._rows (стиль test_dossier_batch): вернёт список
    (sql, params) всех запросов, выполненных под патчем."""
    real = GyrReader._rows
    captured: list[tuple[str, tuple]] = []

    def spy(self, sql, params=()):
        captured.append((" ".join(sql.split()), tuple(params)))
        return real(self, sql, params)

    monkeypatch.setattr(GyrReader, "_rows", spy)
    return captured


def _plan_of(reader: GyrReader, sql: str, params: tuple) -> str:
    """EXPLAIN QUERY PLAN одной строкой."""
    rows = reader.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(r[3] for r in rows)


def _assert_indexed(plan: str, needs_index: bool = True) -> None:
    """Нет полного перебора таблиц данных; при needs_index — есть SEARCH
    по настоящему индексу (USING INDEX), а не только по rowid."""
    assert not _SCAN_OF_DATA.search(plan), f"полный скан таблицы данных: {plan}"
    if needs_index:
        assert "USING INDEX" in plan, f"запрос не использует индекс: {plan}"


# ------------------------------------------------------------ семантика

def test_callers_finds_multi_dot_callee(mini_reader):
    """callers('ЭкспортнаяФункция') находит ОБА квалифицированных ребра:
    «Объект.Модуль.Имя» (2 точки, id 20) и «Модуль.Имя» (1 точка, id 21),
    плюс точное имя (id 2). Регрессия на запрет short-семантики (срез по
    первой точке), аудит PERF-2."""
    rows = mini_reader.callers("ЭкспортнаяФункция")
    assert {r["caller_id"] for r in rows} == {2, 3, 4}


def test_resolve_callee_short_and_qualified(mini_reader):
    """resolve_callee: ловит short-имя из квалифицированного вызова
    («Модуль.Имя» → «Имя»); точное имя и приоритет совпадения сохранены."""
    qualified = mini_reader.resolve_callee("ТестМодуль.ЭкспортнаяФункция")
    exact = mini_reader.resolve_callee("ЭкспортнаяФункция")
    assert [r["id"] for r in qualified] == [1]
    assert [r["id"] for r in exact] == [1]


# ---------------------------------------------------------------- планы

def test_plan_callers_uses_suffix_index(monkeypatch, tmp_path):
    """План callers: MULTI-INDEX OR по idx_calls_callee + суффиксному
    idx_calls_callee_last; SCAN calls недопустим (аудит PERF-2)."""
    reader = GyrReader(_plan_db(tmp_path))
    try:
        captured = _capture_queries(monkeypatch)
        reader.callers("Метод1")
        sql, params = captured[-1]
        plan = _plan_of(reader, sql, params)
        _assert_indexed(plan)
        assert "idx_calls_callee_last" in plan, plan
    finally:
        reader.close()


def test_plan_resolve_callee_uses_name_index(monkeypatch, tmp_path):
    """План resolve_callee: SEARCH по idx_meth_name (compass-44g снял
    перевёрнутый LIKE-скан methods); SCAN methods недопустим."""
    reader = GyrReader(_plan_db(tmp_path))
    try:
        captured = _capture_queries(monkeypatch)
        reader.resolve_callee("Модуль1.Метод1")
        sql, params = captured[-1]
        plan = _plan_of(reader, sql, params)
        _assert_indexed(plan, needs_index=False)
        assert "idx_meth_name" in plan, plan
    finally:
        reader.close()


def test_plan_repomap_queries_no_full_scan(monkeypatch, tmp_path):
    """Планы repomap (входящие рёбра + выборка methods): ходят по индексам;
    SCAN calls/methods на полной выборке недопустим (аудит P36)."""
    from src.facade import repomap as rm
    from src.facade import tools
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    rm._cache.clear()
    try:
        captured = _capture_queries(monkeypatch)
        rows = rm.repomap("ОбъектТест", top=5)
        assert isinstance(rows, list) and rows
    finally:
        rm._cache.clear()
        tools.reset_reader()
    probe = GyrReader(_plan_db(tmp_path))
    try:
        incoming = [(s, p) for s, p in captured
                    if "callee_name COLLATE NOCASE) IN" in s]
        assert incoming, "запрос входящих рёбер не найден среди captured"
        incoming_plan = _plan_of(probe, incoming[0][0], incoming[0][1])
        _assert_indexed(incoming_plan)
        assert "idx_calls_callee_last" in incoming_plan, incoming_plan
        methods = [(s, p) for s, p in captured if "WHERE m.id IN" in s]
        assert methods, "запрос выборки methods не найден среди captured"
        _assert_indexed(_plan_of(probe, methods[0][0], methods[0][1]),
                        needs_index=False)
    finally:
        probe.close()


# ---------------------------------------------------------- объём P36

def test_repomap_fetches_only_rank_nodes(monkeypatch, tmp_path):
    """Выборка methods — только узлы ранга, не вся таблица (P36): в мини
    9 методов, у объекта 1 seed + 1 сосед — запрос обязан нести 2 id."""
    from src.facade import repomap as rm
    from src.facade import tools
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    rm._cache.clear()
    try:
        captured = _capture_queries(monkeypatch)
        rows = rm.repomap("ОбъектТест", top=5)
        assert isinstance(rows, list) and rows
    finally:
        rm._cache.clear()
        tools.reset_reader()
    methods = [(s, p) for s, p in captured if "WHERE m.id IN" in s]
    assert len(methods) == 1
    with sqlite3.connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
    assert total == 9  # 5 базовых + 4 расширений
    params = methods[0][1]
    assert 0 < len(params) < total, (
        f"выборка methods несёт {len(params)} id из {total} — вся таблица?")
