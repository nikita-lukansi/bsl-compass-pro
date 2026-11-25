# -*- coding: utf-8 -*-
"""Тесты паузы векторизации (SemanticIndexer) и delete_config."""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.registry import add_config, delete_config  # noqa: E402
from src.semantic.indexer import SemanticIndexer  # noqa: E402


# ----------------------------------------------------------- пауза индексатора

class FakeEmbeddings:
    def __init__(self):
        self.calls = 0
        self.batch_size = 2

    def embed_documents(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class FakeCollection:
    def __init__(self):
        self.upserted = []
        self.name = "code"  # для to_qdrant_id в resume-сравнении (compass-ehr)

    def scroll_ids(self, limit=256, offset=None, scroll_batch=256):
        return ([], None)  # пустая коллекция — resume не пропускает ничего

    def get(self, limit=256, offset=None, scroll_batch=256):
        return {"ids": [], "documents": [], "metadatas": [],
                "next_offset": None}

    def upsert(self, ids, documents, metadatas, embeddings):
        self.upserted.extend(ids)


def _docs(n):
    return [{"source": f"f{i}.bsl", "name": f"m{i}", "text": f"т{i}",
             "metadata": {}} for i in range(n)]


def test_pause_blocks_flush_until_resumed():
    """Пауза (событие сброшено): embed не вызывается; set() — всё уходит."""
    emb = FakeEmbeddings()
    coll = FakeCollection()
    pause = threading.Event()  # не set = пауза
    indexer = SemanticIndexer(coll, emb, log=lambda s: None,
                              pause_event=pause)

    done = threading.Event()
    result: dict = {}

    def run():
        result["stats"] = indexer.index_iterable(_docs(4))
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.2)
    assert emb.calls == 0          # пауза держит: батчи не отправляются
    assert not done.is_set()
    pause.set()                    # resume: событие = разрешение работать
    assert done.wait(timeout=5)
    assert result["stats"]["indexed"] == 4
    assert emb.calls == 2          # 4 док. / batch 2 = два вызова
    t.join(timeout=1)


def test_no_pause_event_runs_cleanly():
    """Без pause_event поведение прежнее (обратная совместимость)."""
    emb = FakeEmbeddings()
    coll = FakeCollection()
    indexer = SemanticIndexer(coll, emb, log=lambda s: None)
    stats = indexer.index_iterable(_docs(3))
    assert stats["indexed"] == 3
    assert emb.calls == 2  # батчи 2+1


# ----------------------------------------------------------- delete_config

@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    # (source_root без Configuration.xml — config_info будет пуст, это ок)
    return settings_dir


def test_delete_unbinds_sessions_and_default(workspace_dir):
    """Удаление: дефолт сбрасывается, чаты отвязываются, индексы не тронуты."""
    r = add_config("UT", "/dumps/ut", display_name="УТ 11.5")
    cid = r["id"]
    ws.save_settings({"default_config_id": cid})
    ws.save_sessions({"sessions": {
        "chat-a": {"config_id": cid, "updated_at": "2026-11-06T00:00:00+00:00"},
        "chat-b": {"config_id": "Compass_KA_00000000"},
    }})

    result = delete_config(cid)
    assert result["deleted"] == cid
    assert result["was_default"] is True
    assert result["unbound_sessions"] == ["chat-a"]
    # чистка во всех трёх файлах
    assert cid not in (ws.load_registry().get("configs") or {})
    assert ws.load_settings().get("default_config_id") is None
    assert "chat-a" not in ws.load_sessions().get("sessions", {})
    assert "chat-b" in ws.load_sessions().get("sessions", {})


def test_delete_missing_is_error(workspace_dir):
    assert "error" in delete_config("Compass_NO_00000000")


def test_delete_keeps_default_if_other(workspace_dir):
    """Удаление не-дефолтной конфигурации дефолт не трогает."""
    first = add_config("UT", "/dumps/ut")["id"]
    second = add_config("KA", "/dumps/ka")["id"]
    ws.save_settings({"default_config_id": first})
    # short_name KA уже... second добавлен — ок, разные short_name
    result = delete_config(second)
    assert result["was_default"] is False
    assert ws.load_settings().get("default_config_id") == first
