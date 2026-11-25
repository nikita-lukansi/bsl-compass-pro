# -*- coding: utf-8 -*-
"""Perf-контракт repomap (compass-t3j): синтетика 2 000 методов × 20k рёбер.

Инвариант: рейтинг/топ-N НЕ меняется (сравнение с эталоном без раннего
стопа и с прежним OR-запросом входящих рёбер). Метрики улучшения — не
секунды (CI-шум), а детерминированные счётчики: 0 SQL-запросов при смене
top и меньше итераций pagerank при сходимости.
"""
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import repomap as rm  # noqa: E402
from src.facade import tools  # noqa: E402
from src.recon.gyr_reader import CALLEE_LAST  # noqa: E402

OBJ = "ПерфОбъект"
N_OBJ_METHODS = 300
N_OUTER_METHODS = 1700
N_EDGES = 20000

SCHEMA = """
CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE NOT NULL,
    category TEXT, object_name TEXT, module_type TEXT, form_name TEXT,
    is_form INTEGER DEFAULT 0, mtime REAL, size INTEGER);
CREATE TABLE methods (id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules(id), name TEXT NOT NULL,
    type TEXT NOT NULL, is_export INTEGER DEFAULT 0, params TEXT,
    line INTEGER, end_line INTEGER, loc INTEGER);
CREATE TABLE calls (id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL REFERENCES methods(id),
    callee_name TEXT NOT NULL, line INTEGER, callee_key TEXT,
    resolution TEXT NOT NULL, confidence REAL NOT NULL);
-- зеркала ddl.rs (без них perf-замер врал бы: планы не те)
CREATE INDEX idx_mod_object ON modules(object_name);
CREATE INDEX idx_meth_module ON methods(module_id);
CREATE INDEX idx_meth_name ON methods(name COLLATE NOCASE);
CREATE INDEX idx_calls_caller ON calls(caller_id);
CREATE INDEX idx_calls_callee ON calls(callee_name COLLATE NOCASE);
CREATE INDEX idx_calls_callee_last ON calls(
    (substr(callee_name, length(rtrim(callee_name, replace(callee_name, '.', ''))) + 1)) COLLATE NOCASE);
"""


def make_perf_db(tmp_path: Path) -> Path:
    """Синтетический индекс: объект из 300 методов, 1 700 внешних,
    20k рёбер с точными/квалифицированными/регистровыми ловушками."""
    rng = random.Random(20261123)
    conn = sqlite3.connect(tmp_path / "perf_index.db")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO modules VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, "Documents/ПерфОбъект/Ext/ObjectModule.bsl", "Documents",
          OBJ, "ObjectModule", None, 0, 0, 0),
         (2, "Documents/ПерфОбъект/Ext/ManagerModule.bsl", "Documents",
          OBJ, "ManagerModule", None, 0, 0, 0),
         (3, "CommonModules/ПерфБиблиотека/Ext/Module.bsl", "CommonModules",
          "ПерфБиблиотека", "Module", None, 0, 0, 0),
         (4, "CommonModules/ПерфСлужебный/Ext/Module.bsl", "CommonModules",
          "ПерфСлужебный", "Module", None, 0, 0, 0)])
    obj_names = [f"ПерфМетод{i:04d}" for i in range(N_OBJ_METHODS)]
    rows = [(i + 1, 1 if i % 2 else 2, obj_names[i],
             "Функция" if i % 3 else "Процедура", i % 2, "()", 1, 9, 9)
            for i in range(N_OBJ_METHODS)]
    outer_names = [f"ВнешнийМетод{i:04d}" for i in range(N_OUTER_METHODS)]
    rows += [(N_OBJ_METHODS + i + 1, 3 if i % 2 else 4, outer_names[i],
              "Функция", 1, "()", 1, 5, 5) for i in range(N_OUTER_METHODS)]
    conn.executemany("INSERT INTO methods VALUES (?,?,?,?,?,?,?,?,?)", rows)

    calls = []
    cid = 0
    # исходящие: методы объекта зовут внешних (часть — квалифицированно)
    for _ in range(N_EDGES // 2):
        cid += 1
        src = rng.randrange(1, N_OBJ_METHODS + 1)
        name = rng.choice(outer_names)
        callee = name if rng.random() < 0.5 else f"ОбщМодуль.{name}"
        calls.append((cid, src, callee, 1, None, "resolved", 1.0))
    # входящие: внешние зовут методы объекта — точным именем, «Модуль.Имя»,
    # «Объект.Модуль.Имя», плюс регистровая ловушка (NOCASE-мэтч, отсекается
    # python-фильтром) и дубликаты (вес ребра)
    for _ in range(N_EDGES // 2):
        cid += 1
        src = N_OBJ_METHODS + rng.randrange(1, N_OUTER_METHODS + 1)
        name = rng.choice(obj_names)
        r = rng.random()
        if r < 0.35:
            callee = name
        elif r < 0.6:
            callee = f"МодульСлужебный.{name}"
        elif r < 0.75:
            callee = f"Документы.ПерфОбъект.Модуль.{name}"
        elif r < 0.85:
            callee = name.lower()  # ловушка регистра: не должна давать ребро
        else:
            callee = name  # дубликат — вес ребра
        calls.append((cid, src, callee, 1, None, "resolved", 1.0))
    conn.executemany("INSERT INTO calls VALUES (?,?,?,?,?,?,?)", calls)
    conn.commit()
    conn.close()
    return tmp_path / "perf_index.db"


@pytest.fixture
def perf(monkeypatch, tmp_path):
    """Perf-индекс как активный; кэши чистятся до и после (герметично)."""
    db = make_perf_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    rm._cache.clear()
    yield db
    rm._cache.clear()
    tools.reset_reader()


# ------------------------------------------------- эквивалентность SQL

def test_incoming_union_all_equals_or(perf):
    """UNION ALL + NOT IN даёт то же множество строк (с кратностью!),
    что прежний OR двух IN — переформулировка под индексы ничего
    не теряет и не дублирует (compass-t3j)."""
    reader = tools.reader()
    seed_names = {r["name"] for r in reader._rows(
        """SELECT m.name FROM methods m JOIN modules mo ON mo.id = m.module_id
           WHERE mo.object_name = ? COLLATE NOCASE""", (OBJ,))}
    assert len(seed_names) == N_OBJ_METHODS
    names = sorted(seed_names)
    ph = ",".join("?" * len(names))
    old_sql = (f"""SELECT caller_id, callee_name FROM calls
                 WHERE (callee_name COLLATE NOCASE) IN ({ph})
                    OR ({CALLEE_LAST} COLLATE NOCASE) IN ({ph})""")
    old_rows = reader._rows(old_sql, (*names, *names))
    new_sql = (f"""SELECT caller_id, callee_name FROM calls
                WHERE (callee_name COLLATE NOCASE) IN ({ph})
                UNION ALL
                SELECT caller_id, callee_name FROM calls
                WHERE ({CALLEE_LAST} COLLATE NOCASE) IN ({ph})
                  AND (callee_name COLLATE NOCASE) NOT IN ({ph})""")
    new_rows = reader._rows(new_sql, (*names, *names, *names))
    c_old = Counter((r["caller_id"], r["callee_name"]) for r in old_rows)
    c_new = Counter((r["caller_id"], r["callee_name"]) for r in new_rows)
    assert c_new == c_old
    # и после python-фильтра _object_graph — то же множество рёбер
    keep = lambda r: r["callee_name"] in seed_names or \
        r["callee_name"].rsplit(".", 1)[-1] in seed_names  # noqa: E731
    assert Counter(map(keep, new_rows)) == Counter(map(keep, old_rows))


# ------------------------------------------------- инвариант результата

def test_repomap_invariant_top(perf):
    """Топ repomap (ранний стоп eps=3e-5) идентичен эталону без стопа
    (полные 20 итераций) — тот же порядок, те же score в 3 знаках."""
    reader = tools.reader()
    adjacency, seeds, _ = rm._object_graph(reader, OBJ)
    assert len(seeds) == N_OBJ_METHODS
    rank_ref = rm.personalized_pagerank(adjacency, seeds, eps=0.0)
    best = max(rank_ref.values()) or 1.0
    ids = sorted(rank_ref)
    ph = ",".join("?" * len(ids))
    by_id = {r["id"]: r for r in reader._rows(
        f"SELECT m.id, m.name FROM methods m WHERE m.id IN ({ph})", tuple(ids))}
    ref = [(by_id[uid]["name"], round(score / best, 3))
           for uid, score in sorted(rank_ref.items(),
                                     key=lambda kv: kv[1], reverse=True)[:10]]
    got = [(r["name"], r["score"]) for r in rm.repomap(OBJ, top=10)]
    assert got == ref
    # смена top — срез того же рейтинга, а не пересчёт
    assert [(r["name"], r["score"]) for r in rm.repomap(OBJ, top=20)] == ref + [
        (by_id[uid]["name"], round(score / best, 3))
        for uid, score in sorted(rank_ref.items(),
                                 key=lambda kv: kv[1], reverse=True)[10:20]]


def test_repomap_top_change_zero_sql(perf, monkeypatch):
    """Метрика улучшения №1: после cold-расчёта смена top — 0 SQL-запросов
    (прежде (объект, top) = полный пересчёт графа на каждый top)."""
    assert rm.repomap(OBJ, top=10)
    reader = tools.reader()
    counted = {"n": 0}
    orig = reader._rows

    def counting(sql, params=()):
        counted["n"] += 1
        return orig(sql, params)

    monkeypatch.setattr(reader, "_rows", counting)
    rm.repomap(OBJ, top=5)
    rm.repomap(OBJ, top=20)
    assert counted["n"] == 0


# ------------------------------------------------- ранний стоп pagerank

def _random_graph(seed, n_nodes=2000, n_edges=20000, n_seeds=300):
    rng = random.Random(seed)
    nodes = list(range(1, n_nodes + 1))
    seeds = set(rng.sample(nodes, n_seeds))
    hubs = rng.sample(nodes, 10)
    adjacency: dict[int, list[int]] = {}
    for _ in range(n_edges):
        src = rng.choice(nodes)
        r = rng.random()
        dst = rng.choice(hubs) if r < 0.25 else (
            src % n_nodes + 1 if r < 0.5 else rng.choice(nodes))
        adjacency.setdefault(src, []).append(dst)
    return adjacency, seeds


@pytest.mark.parametrize("seed", [42, 7, 2026, 11, 999])
def test_pagerank_early_stop_top_invariant(seed):
    """Топ-20 (score в 3 знаках) при рабочем eps == эталону eps=0.0
    на пяти независимых графах — стоп не меняет рейтинг."""
    adjacency, seeds = _random_graph(seed)
    fast = rm.personalized_pagerank(adjacency, seeds)
    ref = rm.personalized_pagerank(adjacency, seeds, eps=0.0)

    def top20(rank):
        best = max(rank.values()) or 1.0
        return [(u, round(v / best, 3)) for u, v in
                sorted(rank.items(), key=lambda kv: kv[1], reverse=True)[:20]]

    assert top20(fast) == top20(ref)
    assert pytest.approx(sum(fast.values()), abs=1e-6) == 1.0


def test_pagerank_early_stop_faster():
    """Метрика улучшения №2: сходимость экономит итерации — медиана
    времени с eps=3e-5 строго меньше полного цикла (20 итераций)."""
    adjacency, seeds = _random_graph(42)

    def run(eps):
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            rm.personalized_pagerank(adjacency, seeds, eps=eps)
            times.append(time.perf_counter() - t0)
        times.sort()
        return times[len(times) // 2]

    fast = run(rm._PPR_EPS)
    full = run(0.0)
    assert fast < full


def test_repomap_cold_budget(perf):
    """Мягкий бюджет cold-расчёта на 2 000 × 20k (с запасом ×10 от
    ожидания; хрупким не считается — порог не граничный)."""
    rm._cache.clear()
    t0 = time.perf_counter()
    rows = rm.repomap(OBJ, top=10)
    elapsed = time.perf_counter() - t0
    assert isinstance(rows, list) and len(rows) == 10
    assert elapsed < 15.0, f"cold repomap {elapsed:.1f}с > 15с"
