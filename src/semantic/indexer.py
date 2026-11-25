# -*- coding: utf-8 -*-
"""Индексатор векторов: чанки -> эмбеддинги -> Qdrant, с возобновлением.

Упрощение против Community-стейджинга (тот вырос из платной векторизации
через нестабильный шлюз): здесь прогресс переживает рестарт проверкой
существующих ID в коллекции — пропускаем то, что уже встроено.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable

from .embeddings_client import EmbeddingsClient
from .vector_store import VectorCollection, to_qdrant_id

# Подряд неудачных батчей до авто-паузы (PERF-7/compass-ehr)
_FAIL_LIMIT = 5


def chunk_id(source: str, name: str, index: int = 0) -> str:
    """Стабильный ID чанка: источник + имя + позиция (без контента!)."""
    raw = f"{source}::{name}::{index}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


class SemanticIndexer:
    """Пакетная векторизация списка документов в коллекцию.

    resume: перед стартом собирает существующие ID коллекции (scroll) и
    пропускает их — повторный прогон после сбоя продолжает с места обрыва.
    pause_event: СБРОШЕННОЕ событие блокирует отправку батчей на эмбеддинги
    (деньги не тратятся); set() = работа разрешена, clear() = пауза.
    """

    def __init__(self, collection: VectorCollection, embeddings: EmbeddingsClient,
                 log: Callable[[str], None] = print,
                 pause_event: threading.Event | None = None):
        self.collection = collection
        self.embeddings = embeddings
        self.log = log
        self.pause_event = pause_event
        self._consecutive_fails = 0

    def _wait_if_paused(self) -> None:
        """Заблокировать поток, пока пауза не снята (resume → set)."""
        if self.pause_event is not None:
            self.pause_event.wait()  # clear = пауза, set = работать

    def _on_batch_failure(self, exc: Exception, stats: dict[str, Any]) -> None:
        """Подряд неудачные батчи: после 5 — авто-пауза тем же механизмом
        pause_event, что кнопка панели (поток замрёт перед следующим батчем,
        деньги не тратятся), и причина в last_stats (PERF-7/compass-ehr).
        Различение ретраев живёт в embeddings_client (4xx кроме 429 не
        ретраются) — здесь только учёт подряд; успешный батч сбрасывает."""
        self._consecutive_fails += 1
        if self._consecutive_fails < _FAIL_LIMIT:
            return
        stats["paused_reason"] = f"embedding_failed_x5: {exc}"
        self.log(f"semantic: авто-пауза — {stats['paused_reason']}")
        if self.pause_event is not None:
            self.pause_event.clear()

    def _existing_ids(self) -> set[str]:
        """Существующие ID коллекции для resume — в ПРОСТРАНСТВЕ qdrant-UUID:
        ожидаемые ID пересчитываются to_qdrant_id(name, chunk_id) и
        сравниваются множествами. PERF-5/compass-ehr: перебор идёт
        scroll_ids() без payload — раньше get() тащил полные payload'ы
        (тексты, сотни МБ на 590k точек) ради одного сравнения ID.
        Батч 2048 (R8/A10): 590k точек ≈ 290 scroll-запросов вместо 2300
        при дефолте 256."""
        existing: set[str] = set()
        cursor = None
        while True:
            ids, cursor = self.collection.scroll_ids(limit=2048, offset=cursor,
                                                     scroll_batch=2048)
            if not ids:
                break
            existing.update(ids)
            if cursor is None:
                break
        return existing

    def index_iterable(self, documents, batch: int | None = None) -> dict[str, int]:
        """Потоковая индексация итерируемого документов без списка целиком.

        Память ограничена батчем — для больших конфигураций (227k методов)
        список карточек целиком весил бы сотни МБ. Статистика как в index():
        total/skipped/indexed/failed_batches, resume по существующим ID.
        """
        batch = batch or self.embeddings.batch_size
        stats = {"total": 0, "skipped": 0, "indexed": 0, "failed_batches": 0}
        try:
            existing = self._existing_ids()
        except Exception:
            existing = set()

        chunk: list[tuple[str, dict[str, Any]]] = []

        def flush() -> None:
            if not chunk:
                return
            self._wait_if_paused()
            # resume-сравнение в qdrant-пространстве (PERF-5/compass-ehr):
            # scroll_ids отдаёт qdrant-UUID, chunk_id пересчитывается
            fresh = [(cid, d) for cid, d in chunk
                     if to_qdrant_id(self.collection.name, cid) not in existing]
            stats["skipped"] += len(chunk) - len(fresh)
            stats["total"] += len(chunk)
            if fresh:
                try:
                    vectors = self.embeddings.embed_documents(
                        [d["text"] for _, d in fresh])
                    self.collection.upsert(
                        ids=[cid for cid, _ in fresh],
                        documents=[d["text"] for _, d in fresh],
                        metadatas=[d.get("metadata",
                                         {"name": d["name"],
                                          "source_file": d["source"]})
                                   for _, d in fresh],
                        embeddings=vectors,
                    )
                    stats["indexed"] += len(fresh)
                    self._consecutive_fails = 0
                except Exception as exc:
                    stats["failed_batches"] += 1
                    self.log(f"semantic: батч отклонён ({type(exc).__name__}: "
                             f"{str(exc)[:100]}), пропущено {len(fresh)}")
                    self._on_batch_failure(exc, stats)
            chunk.clear()

        for doc in documents:
            chunk.append((chunk_id(doc["source"], doc["name"]), doc))
            if len(chunk) >= batch:
                flush()
        flush()
        self.log(f"semantic: готово — {stats}")
        return stats

    def index(self, documents: list[dict[str, Any]]) -> dict[str, int]:
        """Документы: [{source, name, text, metadata}] -> статистика.

        Возвращает {total, skipped, indexed, failed_batches}.
        Сбой батча не прерывает прогон: батч помечается failed, идём дальше;
        повторный вызов доиндексирует пропущенное (resume по существующим ID).
        """
        by_id = {chunk_id(d["source"], d["name"]): d for d in documents}
        try:
            existing = self._existing_ids()
        except Exception:
            existing = set()
        todo = [(cid, d) for cid, d in by_id.items()
                if to_qdrant_id(self.collection.name, cid) not in existing]

        stats = {"total": len(by_id), "skipped": len(by_id) - len(todo),
                 "indexed": 0, "failed_batches": 0}
        batch = self.embeddings.batch_size
        t0 = time.monotonic()
        for i in range(0, len(todo), batch):
            self._wait_if_paused()
            part = todo[i : i + batch]
            try:
                vectors = self.embeddings.embed_documents([d["text"] for _, d in part])
                self.collection.upsert(
                    ids=[cid for cid, _ in part],
                    documents=[d["text"] for _, d in part],
                    metadatas=[d.get("metadata", {"name": d["name"], "source_file": d["source"]})
                               for _, d in part],
                    embeddings=vectors,
                )
                stats["indexed"] += len(part)
                self._consecutive_fails = 0
            except Exception as exc:
                stats["failed_batches"] += 1
                self.log(f"semantic: батч {i // batch + 1} отклонён "
                         f"({type(exc).__name__}: {str(exc)[:120]}), пропущено {len(part)}")
                self._on_batch_failure(exc, stats)
            if (i // batch) % 10 == 0 and i:
                done = stats["skipped"] + stats["indexed"]
                self.log(f"semantic: {done}/{stats['total']} "
                         f"({time.monotonic() - t0:.0f} с)")
        self.log(f"semantic: готово — {stats}")
        return stats
