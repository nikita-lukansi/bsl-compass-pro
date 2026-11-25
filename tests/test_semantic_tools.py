# -*- coding: utf-8 -*-
"""Тесты семантических тулов: гварды честности + поиск на фейках."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# С7 (аудит публикации): живые пути — только env, без дефолтов владельца
# (нет env — гейт _LIVE закрыт, живые тесты честно скипаются)
_DB = os.environ.get("COMPASS_GYR_DB", "")
_SRC = os.environ.get("COMPASS_SOURCE_ROOT", "")
_LIVE = bool(_DB and _SRC) and Path(_DB).exists() and Path(_SRC).exists()

from src.facade import semantic_tools, tools  # noqa: E402


def test_codesearch_guards_when_not_configured(monkeypatch):
    """Провайдер не задан → честная error, не пустота и не падение."""
    from src import config as cfg
    monkeypatch.setattr(cfg.Config, "semantic_ready",
                        property(lambda self: False))
    # compass-sm4: без активной конфигурации — режимная ветка не задевается
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    res = semantic_tools.codesearch("запрос")
    assert isinstance(res, dict) and "error" in res


def test_semantic_status_reflects_config(monkeypatch):
    from src import config as cfg
    monkeypatch.setattr(cfg.Config, "semantic_ready",
                        property(lambda self: False))
    st = semantic_tools.semantic_status()
    assert st["configured"] is False and "points" in st


def test_codesearch_on_fakes(monkeypatch):
    """Полный путь поиска на фейковых коллекции и эмбеддере."""
    if not (_DB and Path(_DB).exists()):
        pytest.skip("индекс спайка недоступен: задайте COMPASS_GYR_DB")
    os.environ.setdefault("COMPASS_INDEX_DB", _DB)
    # compass-sm4: без активной конфигурации — режимная ветка не задевается
    monkeypatch.setattr("src.context.active_config_id", lambda: None)

    class _FakeCollection:
        def __init__(self):
            self.points = []

        def count(self):
            return len(self.points)

        def query(self, query_embeddings=None, n_results=10, where=None,
                  where_document=None):
            q = query_embeddings[0]
            scored = sorted(self.points, key=lambda p: abs(p["v"] - q[0]))
            if where_document and "$contains" in where_document:
                needle = where_document["$contains"]
                scored = [p for p in scored if needle in p["document"]]
            top = scored[:n_results]
            return {"ids": [[p["id"] for p in top]],
                    "documents": [[p["document"] for p in top]],
                    "metadatas": [[p["metadata"] for p in top]],
                    "distances": [[abs(p["v"] - q[0]) for p in top]]}

    class _FakeEmbeddings:
        batch_size = 8
        def embed_query(self, text, timeout=None):
            return [0.9, 0, 0, 0]
        def embed_documents(self, texts):
            return [self.embed_query(t) for t in texts]

    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setattr(semantic_tools, "_sem", {
        "collection": _FakeCollection(), "embeddings": _FakeEmbeddings(),
        "indexing": False, "last_stats": None, "store": None,
    })
    sem = semantic_tools._sem
    sem["collection"].points = [
        {"id": "a", "document": "Метод ПроведениеДокумента — ObjectModule Реализация",
         "metadata": {"name": "ПроведениеДокумента",
                      "full_path": "Documents/X/Ext/ObjectModule.bsl",
                      "module_type": "ObjectModule"}, "v": 0.9},
    ]
    monkeypatch.setattr(tools, "reader", lambda: object())  # индекс «есть»
    res = semantic_tools.codesearch("проведение документа", limit=5)
    assert isinstance(res, list) and res[0]["name"] == "ПроведениеДокумента"
    assert {"name", "full_path", "score"} <= set(res[0])


def test_status_last_stats_always(monkeypatch):
    """R4/A5: semantic_status содержит last_stats всегда, не только при
    неготовом стеке (мок-стек + monkeypatch _sem)."""
    from src import config as cfg

    class _FakeCollection:
        def count(self):
            return 0

    monkeypatch.setattr(cfg.Config, "semantic_ready",
                        property(lambda self: False))
    monkeypatch.setattr(semantic_tools, "_sem", {
        "collection": None, "store": None, "embeddings": None,
        "indexing": False, "last_stats": {"total": 10, "skipped": 1,
                                          "indexed": 9, "failed_batches": 2},
        "pause": __import__("threading").Event(),
    })
    st = semantic_tools.semantic_status()
    assert st["last_stats"] == {"total": 10, "skipped": 1, "indexed": 9,
                                "failed_batches": 2}

    # стек готов: last_stats не теряется
    monkeypatch.setattr(cfg.Config, "semantic_ready",
                        property(lambda self: True))
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setattr(semantic_tools, "_sem", {
        "collection": _FakeCollection(), "store": None, "embeddings": None,
        "indexing": False, "last_stats": {"indexed": 5},
        "pause": __import__("threading").Event(),
    })
    st = semantic_tools.semantic_status()
    assert st["last_stats"] == {"indexed": 5}


def test_codesearch_query_timeout(monkeypatch):
    """F8/xsv: недоступный провайдер → честная ошибка быстро (таймаут
    из env, без длинных ретраев); embed_query получает query timeout."""
    import threading
    from src.semantic import embeddings_client as ec

    timeouts = []

    class _SlowClient:
        def embed_query(self, text, timeout=None):
            timeouts.append(timeout)
            time.sleep(0.2)  # дольше query timeout из env
            raise ec.EmbeddingError("TimeoutError: hung")  # как конвертирует _post

    class _FakeCollection:
        def count(self):
            return 5

    monkeypatch.setenv("SEMANTIC_QUERY_TIMEOUT", "0.05")
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    # compass-sm4: без активной конфигурации — режимная ветка не задевается
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    monkeypatch.setattr(semantic_tools, "_sem", {
        "collection": _FakeCollection(), "embeddings": _SlowClient(),
        "indexing": False, "last_stats": None, "store": None,
        "pause": threading.Event(),
    })
    monkeypatch.setattr(tools, "reader", lambda: object())
    started = time.monotonic()
    out = semantic_tools.codesearch("запрос")
    elapsed = time.monotonic() - started
    assert "error" in out and "semantic_status" in out["error"]
    assert "не ответил за 0.05" in out["error"]
    assert elapsed < 5  # без ретраев — не 135 с
    assert timeouts == [0.05]


# ------------------------------------------- PERF-3/5/7 (compass-ehr)

def test_status_points_from_registry_no_count(monkeypatch):
    """PERF-3: semantic_status читает points из кэша реестра (set_vectors);
    живой exact-count к Qdrant из статусного пути убран — /api/status
    зовёт semantic_status в event loop, сетевой count блокировал панель."""
    import threading
    from src import config as cfg
    from src.semantic.vector_store import VectorCollection

    counts = {"n": 0}
    real = VectorCollection.count

    def spy(self):
        counts["n"] += 1
        return real(self)

    monkeypatch.setattr(VectorCollection, "count", spy)
    monkeypatch.setattr(cfg.Config, "semantic_ready",
                        property(lambda self: True))
    monkeypatch.setattr(cfg.Config, "active_config_id",
                        property(lambda self: "cid-x"))
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setattr("src.registry.get_config",
                        lambda cid: {"vectors": {"points": {"code": 777}}})
    monkeypatch.setattr(semantic_tools, "_sem", {
        "collection": object(), "store": None, "embeddings": None,
        "indexing": False, "last_stats": {"indexed": 9},
        "pause": threading.Event(),
    })
    st = semantic_tools.semantic_status()
    assert counts["n"] == 0, "статус обязан ходить в кэш реестра, не в Qdrant"
    assert st["points"] == 777


def test_scroll_ids_omits_payload():
    """PERF-5: scroll_ids читает только ID — with_payload=False в kwargs
    запроса к клиенту; get() со своим контрактом не затронут."""
    from src.semantic import vector_store as vs

    seen: dict = {}

    class _Point:
        id = "uuid-1"

    class _Client:
        def collection_exists(self, name):
            return True

        def scroll(self, name, limit=256, offset=None, **kwargs):
            seen.update(kwargs)
            return [_Point()], None

    ids, cursor = vs.VectorCollection(_Client(), "cfg_x_code").scroll_ids(limit=10)
    assert ids == ["uuid-1"] and cursor is None
    assert seen.get("with_payload") is False


def test_auto_pause_after_five_failed_batches(monkeypatch):
    """PERF-7: 5 подряд неудачных батчей → авто-пауза (тот же pause_event,
    что кнопка панели) и paused_reason в stats (== last_stats).
    Таймауты фиктивные: без ретраев и реальных sleep."""
    import threading
    from src.semantic import embeddings_client as ec
    from src.semantic.indexer import SemanticIndexer

    class _BrokenEmbeddings:
        batch_size = 1

        def embed_documents(self, texts):
            raise ec.EmbeddingError("HTTP 502: provider down")

    class _EmptyCollection:
        name = "code"

        def scroll_ids(self, limit=256, offset=None, scroll_batch=256):
            return [], None

        def upsert(self, ids, documents, metadatas, embeddings):
            raise AssertionError("батч не должен дойти до upsert")

    # stats индексатора живёт внутри незавершаемого прогона — снимаем
    # живую ссылку на него в момент первого сбоя батча
    recorded: dict = {}
    real_fail = SemanticIndexer._on_batch_failure

    def fail_spy(self, exc, stats):
        recorded["stats"] = stats
        return real_fail(self, exc, stats)

    monkeypatch.setattr(SemanticIndexer, "_on_batch_failure", fail_spy)

    pause = threading.Event()
    pause.set()  # set = работать (как после кнопки панели)
    logs: list[str] = []
    indexer = SemanticIndexer(_EmptyCollection(), _BrokenEmbeddings(),
                              log=logs.append, pause_event=pause)
    done = threading.Event()
    docs = [{"source": f"f{i}.bsl", "name": f"m{i}", "text": f"т{i}",
             "metadata": {}} for i in range(7)]

    def run():
        indexer.index_iterable(docs)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # 5 батчей падают, на пятой — авто-пауза (clear события); шестой батч
    # замирает на _wait_if_paused ДО вызова эмбеддера — деньги не тратятся
    deadline = time.monotonic() + 5
    while pause.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pause.is_set(), "авто-пауза не сработала после 5 неудач"
    assert any("авто-пауза" in s for s in logs)
    assert recorded["stats"]["failed_batches"] == 5
    reason = recorded["stats"]["paused_reason"]
    assert reason.startswith("embedding_failed_x5:") and "HTTP 502" in reason
    assert not done.is_set()  # прогон остановлен паузой, не завершён
