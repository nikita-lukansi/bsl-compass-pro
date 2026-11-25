# -*- coding: utf-8 -*-
"""_object_graph ловит квалифицированные входящие вызовы «Модуль.Имя» (P35)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _mini_db(tmp_path):
    db = tmp_path / "mini.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE methods (id INTEGER PRIMARY KEY, name TEXT, module_id INTEGER);
        CREATE TABLE modules (id INTEGER PRIMARY KEY, object_name TEXT);
        CREATE TABLE calls (caller_id INTEGER, callee_name TEXT);
        INSERT INTO modules VALUES (1, 'Объект1'), (2, 'ДругоеОбъект');
        INSERT INTO methods VALUES (1, 'ИмяА', 1), (2, 'ЧужойМетод', 2);
        INSERT INTO calls VALUES (2, 'ИмяА');      -- точное имя
        INSERT INTO calls VALUES (2, 'Общ.ИмяА');  -- квалифицированное
    """)
    c.commit()
    c.close()
    return db


def test_object_graph_catches_qualified_incoming(tmp_path):
    from src.recon.gyr_reader import GyrReader
    from src.facade.repomap import _object_graph

    r = GyrReader(_mini_db(tmp_path))
    try:
        adjacency, seeds, _ = _object_graph(r, "Объект1")
        assert seeds == {1}
        # оба вызова (точный и квалифицированный) дают ребро 2 -> 1;
        # до фикса квалифицированное ребро терялось (сломанный LIKE)
        edges = adjacency.get(2, [])
        assert len(edges) == 2 and set(edges) == {1}
    finally:
        r.close()
