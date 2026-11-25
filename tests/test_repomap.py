# -*- coding: utf-8 -*-
"""Тесты локальной центральности: математика на синтетике + smoke на УТ."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade.repomap import personalized_pagerank, repomap  # noqa: E402


# ------------------------------------------------------------ математика

def test_pagerank_hub_wins():
    """Хаб (на него ссылаются трое) выше висячего узла."""
    seeds = {1}
    adjacency = {
        1: [2, 3, 4],   # seed зовёт троих
        2: [5], 3: [5], 4: [5],  # все трое зовут хаб 5
        6: [],          # висячий вне графа seed — не в nodes вообще
    }
    rank = personalized_pagerank(adjacency, seeds)
    # seed выигрывает общий топ (телепорт), хаб — лучший СРЕДИ чужих узлов
    assert rank[1] == max(rank.values())
    non_seed = {u: v for u, v in rank.items() if u != 1}
    assert rank[5] == max(non_seed.values())
    assert pytest.approx(sum(rank.values()), abs=1e-6) == 1.0


def test_pagerank_seed_advantage():
    """Seed-узел при прочих равных держится выше чужих узлов."""
    seeds = {1, 2}
    adjacency = {1: [3], 2: [3], 3: [1]}
    rank = personalized_pagerank(adjacency, seeds)
    assert rank[1] > rank[3] or rank[2] > rank[3]  # телепорт даёт seeds фору


def test_pagerank_empty_seeds():
    assert personalized_pagerank({1: [2]}, set()) == {}


# ---------------------------------------------------------------- smoke

@pytest.fixture
def mini(monkeypatch, tmp_path):
    """Мини-индекс + чистый reader-кэш и кэш repomap (герметично, A12/P2)."""
    from src.facade import tools
    from src.facade import repomap as rm
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    rm._cache.clear()
    yield db
    rm._cache.clear()
    tools.reset_reader()


def test_repomap_real_object(mini):
    """Карта объекта на мини-индексе: непуста, отсортирована, seed сверху.

    График mini: ДокЗаказ.ОбработкаПроведения (seed) →
    ТестМодуль.ЭкспортнаяФункция (сосед, outside)."""
    rows = repomap("ДокЗаказ", top=15)
    assert isinstance(rows, list) and rows
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0]["score"] == 1.0
    # в топе есть методы самого объекта, не только соседи
    assert any(not r["outside"] for r in rows[:5])


def test_repomap_cache(mini):
    from src.facade import repomap as rm
    first = rm.repomap("ДокЗаказ", top=10)
    # ключ — (путь индекса, объект) без top (compass-t3j/P1-3a):
    # полный рейтинг, ответ — срез
    assert rm._cache_key("ДокЗаказ") in rm._cache
    assert rm.repomap("ДокЗаказ", top=10) == first
    # смена top — срез того же полного рейтинга
    assert rm.repomap("ДокЗаказ", top=20)[:10] == first


def test_repomap_cache_keyed_by_config(mini, monkeypatch, tmp_path):
    """Смена активной конфигурации → пересчёт рейтинга, не кэш предыдущей
    базы (P1-3a)."""
    from src.facade import repomap as rm
    from src.facade import tools
    from tests.mini_index import make_mini_db
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_db = make_mini_db(other_dir)

    rm.repomap("ДокЗаказ")
    first = rm.repomap("ДокЗаказ")
    assert rm._cache_key("ДокЗаказ") in rm._cache

    monkeypatch.setattr(tools, "active_index_db", lambda: other_db)
    tools.reset_reader()
    counted = {"n": 0}
    orig_graph = rm._object_graph

    def counting_graph(reader, object_name):
        counted["n"] += 1
        return orig_graph(reader, object_name)

    monkeypatch.setattr(rm, "_object_graph", counting_graph)
    try:
        second = rm.repomap("ДокЗаказ")
    finally:
        tools.reset_reader()
    assert counted["n"] == 1  # пересчёт по новой базе — не рейтинг прежней
    assert second == first  # мини-индексы одинаковы — рейтинг совпадает


def test_repomap_top_change_no_sql(mini, monkeypatch):
    """Смена top после первого расчёта не делает ни одного SQL-запроса
    (compass-t3j: раньше (объект, top) — три полных пересчёта)."""
    from src.facade import repomap as rm
    from src.facade import tools
    rm.repomap("ДокЗаказ", top=5)
    reader = tools.reader()
    counted = {"n": 0}
    orig = reader._rows

    def counting(sql, params=()):
        counted["n"] += 1
        return orig(sql, params)

    monkeypatch.setattr(reader, "_rows", counting)
    assert rm.repomap("ДокЗаказ", top=10)
    assert rm.repomap("ДокЗаказ", top=20)
    assert counted["n"] == 0
