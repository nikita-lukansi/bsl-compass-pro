# -*- coding: utf-8 -*-
"""compass-lsx: кодовая часть R1 — квантизация/on_disk при создании
коллекций + rescore=False в поиске (дополнение операции 7vn).

Embedded-режим (local Qdrant) параметры create_collection НЕ хранит
(get_collection отдаёт quantization_config=None — проверено на 1.20),
поэтому факт передачи квантизации проверяется spy на create_collection
поверх реального embedded-клиента: проверяются kwargs, которые в
server-режиме уйдут в прод. Warning о payload-индексах local-режима
допустим (известное ограничение, скринится pytest.ini-подобно).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("qdrant_client")

_ROOT = Path(__file__).parent.parent


@pytest.fixture
def memory_store():
    from qdrant_client import QdrantClient

    return QdrantClient(path=":memory:")


def _spy_create(store, calls: list):
    """Spy-обёртка create_collection поверх реального клиента (стиль
    test_dossier_batch): kwargs пишутся, вызов проходит дальше."""
    real = store.create_collection

    def wrapper(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    return wrapper


def test_new_collection_has_int8_quantization(memory_store, monkeypatch):
    """R1/fa9: новая коллекция создаётся с ScalarQuantization INT8,
    quantile 0.99; RAM-копии по умолчанию НЕТ (always_ram=False — все
    коллекции on_disk, прогрев only активной при config_use);
    SEMANTIC_WARM=off — exotic: всем always_ram=True. Запись/чтение живы."""
    from src.semantic.vector_store import VectorCollection

    calls: list = []
    store = memory_store
    store.create_collection = _spy_create(store, calls)
    col = VectorCollection(store, "quant_code")
    col.upsert(ids=["a"], documents=["текст"], metadatas=[{"name": "a"}],
               embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert len(calls) == 1
    quant = calls[0]["quantization_config"]
    assert quant.scalar.type.name == "INT8"
    assert quant.scalar.quantile == 0.99
    assert quant.scalar.always_ram is False
    assert col.count() == 1  # embedded-режим жив при квантизации

    calls.clear()
    monkeypatch.setenv("SEMANTIC_WARM", "off")
    col2 = VectorCollection(store, "quant_warm_code")
    col2.upsert(ids=["a"], documents=["текст"], metadatas=[{"name": "a"}],
                embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert calls[0]["quantization_config"].scalar.always_ram is True


def test_quantization_env_off(memory_store, monkeypatch):
    """SEMANTIC_QUANTIZATION=off → коллекция без quantization_config."""
    from src.semantic.vector_store import VectorCollection

    monkeypatch.setenv("SEMANTIC_QUANTIZATION", "off")
    calls: list = []
    store = memory_store
    store.create_collection = _spy_create(store, calls)
    col = VectorCollection(store, "plain_code")
    col.upsert(ids=["a"], documents=["текст"], metadatas=[{"name": "a"}],
               embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert "quantization_config" not in calls[0]
    assert col.count() == 1


def test_query_search_params_rescore_false(monkeypatch):
    """Дополнение 7vn: query_points всегда несёт search_params с
    QuantizationSearchParams(rescore=False) — spy на kwargs клиента."""
    from src.semantic.vector_store import VectorCollection

    seen: dict = {}

    class _FakeClient:
        def collection_exists(self, name):
            return True  # мимо ветки создания — только путь поиска

        def query_points(self, name, **kwargs):
            seen.update(kwargs)
            class _R:  # noqa: N801 — локальная заглушка ответа
                points = []  # пустой ответ: разбор в query() не падает
            return _R()

    col = VectorCollection(_FakeClient(), "cfg_x_code")
    out = col.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]])
    assert out["ids"] == [[]]  # честная пустота
    params = seen["search_params"]
    assert params.quantization.rescore is False


def test_ensure_stack_store_on_disk_true():
    """Landmine (analysis-semantic §6): стор в _ensure_stack обязан
    создаваться с on_disk=True — иначе новые коллекции попадают в
    cached-режим с пре-лоадом гигабайт в RAM (строковый ассерт)."""
    text = (_ROOT / "src" / "facade" / "semantic_tools.py").read_text(
        encoding="utf-8")
    assert "on_disk=True" in text
