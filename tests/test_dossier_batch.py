# -*- coding: utf-8 -*-
"""compass-9cw: build_docs на масштабе — батчи и юзабельные индексы.

Корни таймаута на КА (профилирование, bench на фикстуре КА-масштаба:
было 897 с локально):
1. help_pages не имеет индекса по object_name (help.rs:26 — только
   source_id) → каждый объект сканировал все 8730 страниц;
2. горячий запрос по calls сравнивал в BINARY, а idx_calls_callee
   построен с COLLATE NOCASE (ddl.rs:143) → индекс не юзабелен, SCAN
   таблицы вызовов на каждый объект (EXPLAIN: SCAN vs SEARCH, 21 мс
   против 0,4 мс на 200k строк).
Фикс: _fill_details — батч-проходы (справка одним проходом, реквизиты/
методы IN-чанками) + COLLATE NOCASE в запросе вызовов.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.recon.gyr_reader import GyrReader  # noqa: E402
from src.facade import dossier  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def dossier_env(monkeypatch, tmp_path):
    """Мини-индекс как индекс конфигурации (путь досье — во tmp)."""
    db = make_mini_db(tmp_path)
    from src import registry as registry_mod
    from src import context
    cid = "Compass_Batch_00000001"
    monkeypatch.setattr(registry_mod, "get_config", lambda c: {
        "id": cid, "index_db": str(db), "display_name": "Batch",
        "config_info": {}})
    monkeypatch.setattr(context, "active_config_id", lambda: cid)
    monkeypatch.setattr(dossier, "_docs_dir", lambda c: tmp_path / "docs" / c)
    return cid


def test_help_query_runs_once_not_per_object(dossier_env, monkeypatch):
    """Справка вычитывается ОДНИМ проходом: строка запроса без
    per-object WHERE — иначе 8730 сканов на КА."""
    queries: list[str] = []
    real = GyrReader._rows

    def spy(self, sql, params=()):
        queries.append(" ".join(sql.split()))
        return real(self, sql, params)

    monkeypatch.setattr(GyrReader, "_rows", spy)
    report = dossier.build_docs(dossier_env)
    assert report.get("objects_total", 0) >= 3
    help_scans = [q for q in queries if "FROM help_pages" in q]
    assert len(help_scans) == 1, (
        f"справка должна читаться одним проходом, было {len(help_scans)}")
    assert "WHERE" not in help_scans[0]


def test_calls_hot_query_carries_nocase_collation(dossier_env, monkeypatch):
    """Индекс idx_calls_callee построен с COLLATE NOCASE (ddl.rs:143):
    BINARY-сравнение не юзает его (EXPLAIN: SCAN) — запрос обязан нести
    ту же коллацию, иначе каждый объект сканирует таблицу вызовов."""
    queries: list[str] = []
    real = GyrReader._rows

    def spy(self, sql, params=()):
        queries.append(" ".join(sql.split()))
        return real(self, sql, params)

    monkeypatch.setattr(GyrReader, "_rows", spy)
    dossier.build_docs(dossier_env)
    calls_queries = [q for q in queries
                     if "FROM calls" in q and "IN (" in q]
    assert calls_queries, "в каркасе обязан быть запрос «касаются извне»"
    assert all("COLLATE NOCASE IN" in q for q in calls_queries)


def test_details_are_filled(dossier_env):
    """Наполнение не опустело после батчей: реквизиты/методы/справка."""
    report = dossier.build_docs(dossier_env)
    text = Path(report["written"]).read_text(encoding="utf-8")
    assert "реквизитов: " in text
    assert "методов: " in text
    assert "справка: " in text
    # точный подсчёт методов: ОбработкаПроведения в каркасе (entry point)
    assert "ОбработкаПроведения" in text


def test_build_docs_bounded_queries(dossier_env, monkeypatch):
    """Батчи: запросы не растут линейно с числом объектов×таблиц.
    В мини-фикстуре ~10 объектов: до фикса было ~N×5 точечных — теперь
    фиксированное число батчей + N запросов «касаются извне»."""
    count = {"n": 0}
    real = GyrReader._rows

    def spy(self, sql, params=()):
        count["n"] += 1
        return real(self, sql, params)

    monkeypatch.setattr(GyrReader, "_rows", spy)
    dossier.build_docs(dossier_env)
    assert count["n"] < 60, (
        f"слишком много запросов ({count['n']}) — батч сломался?")
