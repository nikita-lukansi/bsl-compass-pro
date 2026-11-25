# -*- coding: utf-8 -*-
"""Локальная центральность (LC): personalized PageRank по подграфу объекта.

Отличие от глобального PageRank (Community): ранжируется ВНУТРИ объекта —
«важное для этого объекта», а не «важное вообще». БСП-хелперы не забивают
топ, считать нечего долго: подграф объекта — сотни-тысячи узлов, итерации
в Python занимают миллисекунды. Результат кэшируется в памяти.
"""
from __future__ import annotations

from typing import Any

from ..recon.gyr_reader import CALLEE_LAST
from . import tools

_ITERATIONS = 20
_DAMPING = 0.85
# Ранний стоп по L1-сходимости (compass-t3j): на синтетике 2 000 узлов ×
# 20k рёбер топ-50 (score в 3 знаках) стабилен с 8-10 итерации, дельта
# 3e-5 достигается на 12-13 — запас ≥ 2 итерации, экономия ~35-40%.
# 0.0 отключает стоп (полные _ITERATIONS — эталон для тестов инварианта).
_PPR_EPS = 3e-5
_CACHE_LIMIT = 32
# Ключ — (путь индекса активного контекста, object_name) БЕЗ top
# (compass-t3j): полный рейтинг объекта считаем один раз, смена top —
# срез без пересчёта (раньше top=5/10/20 — три независимых расчёта по
# ~90 с на больших объектах). Путь в ключе (P1-3a): смена активной
# конфигурации не отдаёт рейтинг предыдущей базы.
_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _cache_key(object_name: str) -> tuple[str, str]:
    """Ключ кэша: путь индекса активного контекста (тот же источник, что
    у tools.reader — active_index_db) + имя объекта."""
    db = tools.active_index_db()
    return (str(db) if db else "", object_name)


def reset_cache() -> None:
    """Сброс кэша рейтингов — после смены конфигурации/пересборки индекса
    (вызывается из registry_tools.reset_runtime_caches, P1-3a): путь может
    остаться прежним, а содержимое индекса — уже новым."""
    _cache.clear()


def personalized_pagerank(
    adjacency: dict[int, list[int]],
    seeds: set[int],
    iterations: int = _ITERATIONS,
    damping: float = _DAMPING,
    eps: float = _PPR_EPS,
) -> dict[int, float]:
    """PageRank с персонализацией: телепорт ТОЛЬКО в seeds.

    Args:
        adjacency: узел -> исходящие соседи (дубликаты допустимы — вес ребра).
        seeds: множество узлов объекта (куда возвращается блуждающий).
        iterations: кап итераций степенного метода.
        damping: вероятность следования по ребру.
        eps: ранний стоп при L1-дельте между итерациями < eps;
            0.0 — всегда полный цикл по iterations.

    Returns:
        {узел: вес}, сумма весов = 1; seeds-узлы получают преимущество.
    """
    if not seeds:
        return {}
    # все участники переданного подграфа (его границы задаёт сборщик графа)
    nodes = set(seeds) | set(adjacency)
    nodes.update(d for dsts in adjacency.values() for d in dsts)
    n = len(nodes) or 1
    rank = {u: 1.0 / n for u in nodes}
    teleport = 1.0 / len(seeds)
    for _ in range(iterations):
        nxt = {u: 0.0 for u in nodes}
        for src in nodes:
            dsts = adjacency.get(src)
            if dsts:
                share = damping * rank[src] / len(dsts)
                for dst in dsts:
                    if dst in nxt:
                        nxt[dst] += share
            # dangling: масса уходит в телепорт (равномерно по seeds)
            else:
                for s in seeds:
                    nxt[s] += damping * rank[src] / len(seeds)
        for s in seeds:
            nxt[s] += (1 - damping) * teleport
        delta = sum(abs(nxt[u] - rank[u]) for u in nodes)
        rank = nxt
        if delta < eps:
            break  # сходимость: дальнейшие итерации не меняют результат
    return rank


def _object_graph(reader, object_name: str) -> tuple[dict[int, list[int]], set[int], dict[int, dict]]:
    """Подграф объекта: seeds (методы объекта) + их соседи по рёбрам.

    Рёбра calls связывают caller_id с callee_name (строкой!) — резолвим
    имена соседей в id по methods в один проход.
    """
    seeds_rows = reader._rows(
        """SELECT m.id, m.name FROM methods m
           JOIN modules mo ON mo.id = m.module_id
           WHERE mo.object_name = ? COLLATE NOCASE""", (object_name,))
    if not seeds_rows:
        return {}, set(), {}
    seed_ids = {r["id"] for r in seeds_rows}
    seed_names = {r["name"] for r in seeds_rows}
    placeholders = ",".join("?" * len(seed_ids))

    # исходящие: что вызывают методы объекта
    out_rows = reader._rows(
        f"SELECT caller_id, callee_name FROM calls WHERE caller_id IN ({placeholders})",
        tuple(seed_ids))
    # входящие: кто вызывает методы объекта — точное имя ИЛИ суффикс после
    # последней точки («Модуль.Имя» / «Объект.Модуль.Имя»). Разбито на
    # UNION ALL двух индексных веток вместо OR (compass-t3j): OR двух IN
    # по разным выражениям давал нестабильный план (SCAN 4,9 млн строк
    # calls с вычислением суффикса на каждой — вот источник ~90 с cold).
    # Ветка 2 гасит пересечение NOT IN-ом, поэтому множество строк
    # равно дизъюнкции (дубликаты рёбер сохранены — они несут вес).
    # Скобочный COLLATE — чтобы IN ушёл по индексам
    # (idx_calls_callee/idx_calls_callee_last); NOCASE не расширяет
    # результат: дешёвый фильтр ниже отсекает регистровые варианты,
    # он же ловит 2+ точки
    names = sorted(seed_names)
    ph = ",".join("?" * len(names))
    in_rows = reader._rows(
        f"""SELECT caller_id, callee_name FROM calls
            WHERE (callee_name COLLATE NOCASE) IN ({ph})
            UNION ALL
            SELECT caller_id, callee_name FROM calls
            WHERE ({CALLEE_LAST} COLLATE NOCASE) IN ({ph})
              AND (callee_name COLLATE NOCASE) NOT IN ({ph})""",
        (*names, *names, *names))
    in_rows = [r for r in in_rows
               if r["callee_name"] in seed_names
               or r["callee_name"].rsplit(".", 1)[-1] in seed_names]

    # резолв имён соседей в id одним пакетом (BINARY-сравнение — как прежде:
    # COLLATE NOCASE здесь изменил бы результат для регистровых имён);
    # однозначность считаем в Python — GROUP BY+HAVING в SQL гнал
    # сортировку по всей methods (653k строк на живой базе)
    neighbor_names = {r["callee_name"].rsplit(".", 1)[-1] for r in out_rows}
    neighbor_names -= seed_names
    id_by_name: dict[str, int] = {r["name"]: r["id"] for r in seeds_rows}
    if neighbor_names:
        ph = ",".join("?" * len(neighbor_names))
        name_ids: dict[str, list[int]] = {}
        for r in reader._rows(
                f"SELECT id, name FROM methods WHERE name IN ({ph})",
                tuple(neighbor_names)):
            name_ids.setdefault(r["name"], []).append(r["id"])
        id_by_name.update(
            {nm: ids[0] for nm, ids in name_ids.items() if len(ids) == 1})

    adjacency: dict[int, list[int]] = {}
    for r in out_rows:
        dst = id_by_name.get(r["callee_name"].rsplit(".", 1)[-1])
        if dst is not None:
            adjacency.setdefault(r["caller_id"], []).append(dst)
    for r in in_rows:
        src = r["caller_id"]
        dst = id_by_name.get(r["callee_name"].rsplit(".", 1)[-1])
        if dst is not None:
            adjacency.setdefault(src, []).append(dst)
    return adjacency, seed_ids, id_by_name


def repomap(object_name: str, top: int = 20) -> list[dict] | dict:
    """Карта объекта: топ-методы по локальной центральности.

    Возвращает [{name, path, module_type, is_export, score}] — score
    нормирован к лучшему; у методов вне объекта помечено outside=true.
    Полный рейтинг объекта кэшируется один раз — смена top берётся
    срезом без пересчёта (compass-t3j); ключ несёт путь индекса контекста
    (P1-3a) — другая активная конфигурация считается заново.
    Каждый ответ несёт config активной конфигурации (compass-yho), когда
    она выбрана: dict-ответы — ключом, list-ответы — завершающим
    элементом-маркером (кэш при этом не мутируется).
    """
    key = _cache_key(object_name)
    if key in _cache:
        return tools._echo_config(_cache[key][:top])
    reader = tools.reader()
    if reader is None:
        return tools._echo_config({"error": "индекс не настроен"})
    adjacency, seeds, id_by_name = _object_graph(reader, object_name)
    if not seeds:
        return tools._echo_config({"_no_matches": True, "object": object_name,
                                   "hint": "методов объекта не найдено; "
                                           "проверь имя"})
    rank = personalized_pagerank(adjacency, seeds)
    best = max(rank.values()) or 1.0
    # только узлы ранга вместо всей таблицы methods (аудит P36, compass-44g)
    ids = sorted(rank.keys())
    ph = ",".join("?" * len(ids))
    rows = reader._rows(
        f"""SELECT m.id, m.name, m.params, m.is_export, mo.rel_path, mo.module_type
           FROM methods m JOIN modules mo ON mo.id = m.module_id
           WHERE m.id IN ({ph})""", tuple(ids))
    by_id = {r["id"]: r for r in rows}
    result = []
    for uid, score in sorted(rank.items(), key=lambda kv: kv[1], reverse=True):
        info = by_id.get(uid)
        if not info:
            continue
        result.append({
            "name": info["name"], "params": info["params"],
            "path": info["rel_path"], "module_type": info["module_type"],
            "is_export": bool(info["is_export"]),
            "outside": uid not in seeds,
            "score": round(score / best, 3),
        })
    while len(_cache) >= _CACHE_LIMIT:
        _cache.pop(next(iter(_cache)))  # FIFO-вытеснение старейшего (аудит PERF-1)
    _cache[key] = result
    # compass-yho: бейдж — в копию-срез, кэш остаётся без привязки к контексту
    return tools._echo_config(result[:top])
