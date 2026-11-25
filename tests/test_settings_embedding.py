# -*- coding: utf-8 -*-
"""Тесты глобальной настройки эмбеддинг-модели: config.embedding_settings
(settings.json переопределяет env), semantic_ready, reset_semantic_stack,
VectorStore.delete_prefixed_code."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.config import config  # noqa: E402


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Рабочая папка в tmp; env-эмбеддинг через атрибуты config-синглтона
    (они заморожены на импорте — сеттить env бессмысленно)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setattr(config, "embedding_base_url", "http://env:8000/v1")
    monkeypatch.setattr(config, "embedding_model", "m-env")
    monkeypatch.setattr(config, "embedding_api_key", "key-env")
    return settings_dir


def test_env_defaults_when_no_override(workspace_dir):
    """Без override в settings.json — env-значения, source=env."""
    emb = config.embedding_settings()
    assert emb == {"base_url": "http://env:8000/v1", "model": "m-env",
                   "api_key": "key-env", "source": "env"}


def test_settings_override_wins(workspace_dir):
    """Полный override: settings.json сильнее env."""
    ws.save_settings({"embedding": {
        "base_url": "http://kodik:8000/v1", "model": "qwen3-embedding-4b",
        "api_key": "key-new"}})
    emb = config.embedding_settings()
    assert emb["model"] == "qwen3-embedding-4b"
    assert emb["base_url"] == "http://kodik:8000/v1"
    assert emb["api_key"] == "key-new"
    assert emb["source"] == "settings"


def test_partial_override_keeps_env_gaps(workspace_dir):
    """Частичный override: только модель — base_url/api_key из env;
    один api_key источником не считается."""
    ws.save_settings({"embedding": {"model": "m-file", "api_key": "k2"}})
    emb = config.embedding_settings()
    assert emb["model"] == "m-file"
    assert emb["base_url"] == "http://env:8000/v1"
    assert emb["api_key"] == "k2"
    assert emb["source"] == "settings"

    ws.save_settings({"embedding": {"api_key": "k3"}})
    emb = config.embedding_settings()
    assert emb["model"] == "m-env"  # модель из env
    assert emb["source"] == "env"  # api_key сам источник не меняет


def test_semantic_ready_follows_override(workspace_dir, monkeypatch):
    """semantic_ready: env пуст → False; override в settings.json → True."""
    monkeypatch.setattr(config, "embedding_base_url", "")
    monkeypatch.setattr(config, "embedding_model", "")
    assert config.semantic_ready is False
    ws.save_settings({"embedding": {"base_url": "http://x/v1", "model": "m"}})
    assert config.semantic_ready is True


def test_reset_semantic_stack_keeps_indexing():
    """reset обнуляет стек, но не трогает флаг идущей индексации."""
    from src.facade import semantic_tools as st
    st._sem.update({"store": "s", "collection": "c", "embeddings": "e",
                    "indexing": True, "last_stats": {"indexed": 5}})
    try:
        st.reset_semantic_stack()
        assert st._sem["store"] is None
        assert st._sem["collection"] is None
        assert st._sem["embeddings"] is None
        assert st._sem["indexing"] is True  # идущая векторизация живёт
    finally:
        st._sem.update({"store": None, "collection": None,
                        "embeddings": None, "indexing": False,
                        "last_stats": None})


def test_delete_prefixed_code_filters():
    """Удаляются только cfg_*_code; прочие коллекции не тронуты."""
    from src.semantic.vector_store import VectorStore

    class FakeClient:
        def __init__(self, names):
            self.names = names
            self.deleted = []

        class _Colls:
            def __init__(self, names):
                self.collections = [type("C", (), {"name": n})() for n in names]

        def get_collections(self):
            return FakeClient._Colls(self.names)

        def delete_collection(self, name):
            self.deleted.append(name)
            self.names.remove(name)

    client = FakeClient(["cfg_Compass_KA_x_code", "cfg_Compass_UT_y_code",
                         "help_articles", "cfg_something_else"])
    store = VectorStore.__new__(VectorStore)  # без реального QdrantClient
    store.client = client
    deleted = store.delete_prefixed_code()
    assert sorted(deleted) == ["cfg_Compass_KA_x_code", "cfg_Compass_UT_y_code"]
    assert client.names == ["help_articles", "cfg_something_else"]
