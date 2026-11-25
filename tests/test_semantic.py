# -*- coding: utf-8 -*-
"""Тесты семантического слоя Pro: RRF, клиент эмбеддингов (ретраи, батчи),
гибридный поиск на моках, индексатор с resume, vector_store in-memory."""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.semantic.embeddings_client import EmbeddingsClient, EmbeddingError, split_camel_case
from src.semantic.hybrid_search import _QUERY_INSTRUCT_DEFAULT, HybridSearch, rrf_fuse
from src.semantic.indexer import SemanticIndexer, chunk_id
from src.semantic.vector_store import to_qdrant_id


# ---------------------------------------------------------------- RRF

def test_rrf_fuse_math():
    """Ранги 0-based: вхождение из двух списков выше одиночного топа."""
    a = ["x", "y"]
    b = ["y", "z"]
    fused = rrf_fuse([a, b])
    scores = dict(fused)
    assert scores["y"] == pytest.approx(1 / 61 + 1 / 60)  # rank1 + rank0
    assert fused[0][0] == "y"  # встретился в обоих — первый
    assert scores["x"] == pytest.approx(1 / 60)
    assert scores["z"] == pytest.approx(1 / 61)
    assert scores["x"] > scores["z"]


def test_rrf_fuse_top_k():
    assert len(rrf_fuse([["a", "b", "c"]], top_k=2)) == 2


# ------------------------------------------------------- camelCase / instruct

def test_split_camel_case():
    assert split_camel_case("ОбработкаПроведения") == "Обработка Проведения"
    assert split_camel_case("APIКлиент") == "API Клиент"


def test_query_instruct_toggle(monkeypatch):
    hs = HybridSearch(collection=None, embeddings=None)
    assert hs._query_instruct().startswith("Instruct:")
    monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCT", "")
    assert hs._query_instruct() == ""


def test_query_instruct_on_is_default(monkeypatch):
    """P1-6: «включающие» значения on/1/true/yes (compose-дефолт — on!)
    дают дефолтную инструкцию, а не буквальное слово в запросе."""
    hs = HybridSearch(collection=None, embeddings=None)
    for val in ("on", "1", "true", "yes", "ON", " True "):
        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCT", val)
        assert hs._query_instruct() == _QUERY_INSTRUCT_DEFAULT, val
        assert hs._embed_input("Проверить") == (
            _QUERY_INSTRUCT_DEFAULT + "Проверить"), val
    for val in ("", "0", "off", "none"):
        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCT", val)
        assert hs._query_instruct() == "", val
        assert hs._embed_input("Проверить") == "Проверить", val


# ------------------------------------------------------- клиент эмбеддингов

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _ok_payload(n, dim=4):
    data = []
    for i in range(n):
        vec = [0.0] * dim
        vec[0] = float(i)
        data.append({"embedding": vec})
    return {"data": data}


def test_client_retries_transient(monkeypatch):
    """5xx -> ретраи -> успех; паузы подменены на нули."""
    monkeypatch.setattr("src.semantic.embeddings_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, None)
        return _FakeResponse(_ok_payload(1))

    monkeypatch.setattr("src.semantic.embeddings_client.urllib.request.urlopen", fake_urlopen)
    client = EmbeddingsClient(base_url="http://x", api_key="k", model="m", batch_size=8)
    # F8/xsv: ретраи — индексационный путь (embed_documents); embed_query —
    # один запрос без ретраев (запросный путь не должен висеть)
    vectors = client.embed_documents(["тест"])
    assert calls["n"] == 3 and len(vectors) == 1
    calls["n"] = 0
    with pytest.raises(EmbeddingError):
        client.embed_query("тест")
    assert calls["n"] == 1


def test_client_no_retry_on_4xx(monkeypatch):
    monkeypatch.setattr("src.semantic.embeddings_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("src.semantic.embeddings_client.urllib.request.urlopen", fake_urlopen)
    client = EmbeddingsClient(base_url="http://x", api_key="k", model="m")
    with pytest.raises(EmbeddingError):
        client.embed_query("тест")
    assert calls["n"] == 1  # 4xx не ретраится


def test_client_batches(monkeypatch):
    monkeypatch.setattr("src.semantic.embeddings_client.time.sleep", lambda s: None)
    batches = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        batches.append(len(body["input"]))
        return _FakeResponse(_ok_payload(len(body["input"])))

    monkeypatch.setattr("src.semantic.embeddings_client.urllib.request.urlopen", fake_urlopen)
    client = EmbeddingsClient(base_url="http://x", api_key="k", model="m", batch_size=3)
    vectors = client.embed_documents(["а"] * 7)
    assert batches == [3, 3, 1] and len(vectors) == 7


def test_client_requires_config():
    with pytest.raises(EmbeddingError):
        EmbeddingsClient(base_url="", model="")


# ------------------------------------------------------- гибридный поиск (мок)

class _FakeCollection:
    """Коллекция на словаре: ищет по близости первому элементу вектора."""

    def __init__(self, points):
        self.points = points  # [{id, document, metadata, v}]
        self.name = "code"    # для to_qdrant_id в resume-сравнении (compass-ehr)

    def query(self, query_embeddings=None, n_results=10, where=None, where_document=None):
        q = query_embeddings[0]
        scored = sorted(self.points, key=lambda p: abs(p["v"] - q[0]))
        if where_document and "$contains" in where_document:
            needle = where_document["$contains"]
            scored = [p for p in scored if needle in p["document"]]
        ids = [p["id"] for p in scored[:n_results]]
        return {
            "ids": [ids],
            "documents": [[p["document"] for p in scored[:n_results]]],
            "metadatas": [[p["metadata"] for p in scored[:n_results]]],
            "distances": [[abs(p["v"] - q[0]) for p in scored[:n_results]]],
        }


class _FakeEmbeddings:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def embed_query(self, text, timeout=None):
        return self.mapping.get(text, [0.5, 0, 0, 0])

    batch_size = 8

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


def test_hybrid_single_word_fuses(monkeypatch):
    """Однословный запрос: вектор + буквальный, RRF поднимает совпадение обоих."""
    monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCT", "")
    col = _FakeCollection([
        {"id": "sem", "document": "проверка заполнения семантическая",
         "metadata": {"name": "Семантическая"}, "v": 0.9},
        {"id": "exact", "document": "точный ОбработкаПроведения хвост",
         "metadata": {"name": "Точная"}, "v": 0.1},
    ])
    emb = _FakeEmbeddings({"Обработка Проведения": [0.9, 0, 0, 0]})
    hs = HybridSearch(collection=col, embeddings=emb)
    rows = hs.search("ОбработкаПроведения", limit=2)
    # "exact" найден обеими ветками (вектор ближе к 0.1? нет: к 0.9 ближе sem;
    # буквальная ветка даёт только exact) — RRF сводит обоих в топ
    assert {r["id"] for r in rows} == {"sem", "exact"}
    assert all(0 < r["score"] <= 1.0 for r in rows)


def test_hybrid_multiword_vector_only(monkeypatch):
    monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCT", "")
    col = _FakeCollection([
        {"id": "a", "document": "x", "metadata": {"name": "A"}, "v": 0.5},
    ])
    hs = HybridSearch(collection=col, embeddings=_FakeEmbeddings())
    rows = hs.search("как провести документ", limit=5)
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["name"] == "A"


# ------------------------------------------------------- индексатор + qdrant

def _docs(n):
    return [{"source": f"src/m{i}.bsl", "name": f"Функция{i}",
             "text": f"тело функции {i} проверка", "metadata": {"name": f"Функция{i}"}}
            for i in range(n)]


def test_chunk_id_stable():
    assert chunk_id("/a.bsl", "Ф") == chunk_id("/a.bsl", "Ф")
    assert chunk_id("/a.bsl", "Ф") != chunk_id("/b.bsl", "Ф")


def test_indexer_resume_and_failed_batch():
    """Первый прогон: батч падает一次 -> failed, повторный доиндексирует."""
    col = _FakeCollection([])
    upserts = []

    def fake_upsert(ids, documents, metadatas, embeddings):
        if len(upserts) == 0:
            upserts.append("fail")
            raise RuntimeError("503")
        upserts.append("ok")
        for cid, doc, meta in zip(ids, documents, metadatas):
            col.points.append({"id": cid, "document": doc, "metadata": meta, "v": 0.5})

    col.upsert = fake_upsert
    # resume-перебор идёт scroll_ids (PERF-5/compass-ehr): отдаёт ID в
    # пространстве qdrant-UUID — фейк пересчитывает так же, как upsert
    col.scroll_ids = lambda limit=100, offset=None, scroll_batch=256: (
        [to_qdrant_id("code", p["id"]) for p in col.points], None)
    idx = SemanticIndexer(collection=col, embeddings=_FakeEmbeddings(), log=lambda s: None)
    st1 = idx.index(_docs(3))
    assert st1["failed_batches"] == 1 and st1["indexed"] == 0
    st2 = idx.index(_docs(3))
    assert st2["failed_batches"] == 0 and st2["indexed"] == 3
    assert st2["total"] == 3  # дедуп по chunk_id


def test_vector_store_roundtrip():
    """Полный цикл на embedded Qdrant (скип без пакета)."""
    pytest.importorskip("qdrant_client")
    from src.semantic.vector_store import VectorStore

    store = VectorStore(path=":memory:")
    col = store.collection("code")
    col.upsert(
        ids=["a", "b"],
        documents=["альфа тест", "бета"],
        metadatas=[{"name": "a"}, {"name": "b"}],
        embeddings=[[1.0, 0, 0, 0], [0.0, 1.0, 0, 0]],
    )
    assert col.count() == 2
    res = col.query(query_embeddings=[[1.0, 0, 0, 0]], n_results=2)
    assert res["ids"][0][0] == "a"          # исходный chunk_id, не UUID
    # R8/A10: get с батчем scroll_batch=1 — обе точки, курсор исчерпан
    page = col.get(limit=2, scroll_batch=1)
    assert sorted(page["ids"]) == ["a", "b"]
    assert page["next_offset"] is None
    col.delete(ids=["a"])
    assert col.count() == 1


def test_index_iterable_streaming():
    """Потоковая индексация: 25 документов батчами по 10, генератор."""
    class _Col:
        def __init__(self):
            self.points = []
            self.fail_first = True
            self.name = "code"  # для to_qdrant_id в resume-сравнении (compass-ehr)

        def scroll_ids(self, limit=100, offset=None, scroll_batch=256):
            return ([to_qdrant_id("code", p[0]) for p in self.points], None)

        def get(self, limit=100, offset=None, scroll_batch=256):
            return {"ids": [p[0] for p in self.points], "next_offset": None}

        def upsert(self, ids, documents, metadatas, embeddings):
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("503 первый батч")
            self.points.extend(zip(ids, documents))

    class _Emb:
        batch_size = 10
        def embed_documents(self, texts):
            return [[0.0] * 4 for _ in texts]

    col = _Col()
    idx = SemanticIndexer(collection=col, embeddings=_Emb(), log=lambda s: None)
    docs = ({"source": f"/m{i}.bsl", "name": f"Ф{i}",
             "text": f"тело {i}", "metadata": {"name": f"Ф{i}"}}
            for i in range(25))
    st = idx.index_iterable(docs)
    assert st["failed_batches"] == 1
    assert st["total"] == 25 and st["indexed"] == 15  # 25 - упавший батч(10)
    # повтор: resume пропускает встроенное, доиндексирует упавшее
    docs2 = ({"source": f"/m{i}.bsl", "name": f"Ф{i}",
              "text": f"тело {i}", "metadata": {"name": f"Ф{i}"}}
             for i in range(25))
    st2 = idx.index_iterable(docs2)
    assert st2["indexed"] == 10 and st2["skipped"] == 15
