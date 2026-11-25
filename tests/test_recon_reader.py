# -*- coding: utf-8 -*-
"""Smoke-тесты читателя gyrfalcon на живом индексе спайка (ut115_gyr.db).

Индекс не хранится в репо (1.3 ГБ): тесты берут путь из COMPASS_GYR_DB
(дефолт — папка спайка) и честно скипаются, если файла нет.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.recon.gyr_reader import GyrReader  # noqa: E402

# С7 (аудит публикации): дефолта-пути владельца нет — только env
# COMPASS_GYR_DB; без env модуль честно скипается.
_DB = os.environ.get("COMPASS_GYR_DB", "")
pytestmark = pytest.mark.skipif(
    not (_DB and Path(_DB).exists()),
    reason="индекс спайка недоступен: задайте COMPASS_GYR_DB"
)


@pytest.fixture(scope="module")
def reader():
    r = GyrReader(_DB)
    yield r
    r.close()


def test_readonly_connection(reader):
    """Соединение только на чтение: попытка записи падает, база не тронута."""
    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute("CREATE TABLE _probe(x)")
    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute("DELETE FROM methods WHERE 0")


import sqlite3  # noqa: E402  (после pytestmark намеренно: общий импорт выше)


def test_stats_layers(reader):
    stats = reader.stats()
    assert stats["methods"] > 200_000       # УТ: 227k
    assert stats["calls"] > 1_000_000       # 1.7M рёбер
    assert stats["subsystem_content"] > 10_000
    assert stats["metadata_code_usages"] > 100_000


def test_find_methods_fts(reader):
    rows = reader.find_methods("ОбработкаПроведения", limit=5)
    assert rows and rows[0]["name"] == "ОбработкаПроведения"
    assert rows[0]["rel_path"].endswith(".bsl")
    assert rows[0]["line"] >= 1


def test_callers_edges_with_resolution(reader):
    rows = reader.callers("ОбработкаПроведения", limit=20)
    assert rows
    assert {"caller_name", "resolution", "confidence"} <= set(rows[0])


def test_object_attributes_rich(reader):
    rows = reader.object_attributes("РеализацияТоваровУслуг")
    assert len(rows) > 20
    assert {"attr_name", "attr_type"} <= set(rows[0])


def test_triggers_movements_and_subscriptions(reader):
    """Аналог triggers_on_write: движения + подписки вместе."""
    movements = reader.movements("РеализацияТоваровУслуг")
    assert movements, "у документа УТ должны быть движения"
    subs = reader.event_subscriptions("РеализацияТоваровУслуг")
    # подписок может не быть конкретно на этот объект — просто валидный список
    assert isinstance(subs, list)


def test_subsystems_and_forms(reader):
    subs = reader.subsystems_of("РеализацияТоваровУслуг")
    assert subs, "документ УТ входит в подсистемы"
    forms = reader.forms_of("РеализацияТоваровУслуг")
    assert forms and forms[0]["elements"] > 50


def test_references_and_usages(reader):
    refs = reader.references_to("РеализацияТоваровУслуг", limit=50)
    assert refs, "на документ УТ ссылаются реквизитами"
    usages = reader.code_usages("РеализацияТоваровУслуг", limit=50)
    assert usages


def test_roles_predefined_entry_points(reader):
    assert reader.role_rights("РеализацияТоваровУслуг")
    eps = reader.entry_points()
    assert eps["scheduled_jobs"], "в УТ есть регламентные задания"
    assert eps["http_services"]


def test_read_method_source(reader):
    m = reader.find_methods("ОбработкаПроведения", limit=1)[0]
    # С7: корень выгрузки — только env, без дефолта владельца
    root = os.environ.get("COMPASS_SOURCE_ROOT", "")
    if not (root and Path(root).exists()):
        pytest.skip("дамп УТ недоступен: задайте COMPASS_SOURCE_ROOT")
    body = reader.read_method_source(m["rel_path"], m["line"], m["end_line"], root)
    assert "КонецПроцедуры" in body or "КонецФункции" in body or len(body) > 50


def test_reader_thread_safe(reader):
    """Регрессия e2e: соединение используется из разных потоков (пул FastMCP
    + панель) — раньше падало 'can only be used in that same thread'."""
    import threading

    results: list = []
    errors: list = []

    def query():
        try:
            rows = reader.find_methods("ОбработкаПроведения", limit=3)
            results.append(len(rows))
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=query) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(results) == 6 and all(n > 0 for n in results)


def test_methods_page_covers_all(reader):
    """id-пагинация выдаёт все методы без повторов и пропусков."""
    total = reader.conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
    seen = set()
    last_id = 0
    pages = 0
    while True:
        rows = reader.methods_page(last_id=last_id, limit=1000)
        if not rows:
            break
        ids = [r["id"] for r in rows]
        assert ids == sorted(ids) and ids[0] > last_id
        seen.update(ids)
        last_id = ids[-1]
        pages += 1
        if pages > 500:
            break
    assert len(seen) == total
