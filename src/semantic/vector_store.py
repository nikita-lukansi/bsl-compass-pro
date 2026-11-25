# -*- coding: utf-8 -*-
"""Qdrant-хранилище векторов (авторский код; поведение покрыто тестами
семантики — tests/test_semantic*.py).

Паттерны надёжности:
- ID = uuid5(фиксированный NS, collection + ':' + chunk_id) — детерминированы,
  повторная индексация перезаписывает точку, а не плодит дубли;
- исходный chunk_id хранится в payload и возвращается из get/query
  (UUID необратим, а учёт и дедуп строятся на исходных ID);
- payload-индексы под фильтры, 'document' как text-поле для MatchText;
- scroll-пагинация курсором (next_offset), не числовым индексом.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

_NS = uuid.UUID("9f1c3a52-8d44-4b6e-9a71-2f5e0c8b7d31")

_UPSERT_BATCH = 256
_SCROLL_BATCH = 256

_PAYLOAD_INDEXES = {
    "source_file": "keyword",
    "module_type": "keyword",
    "is_export": "bool",
    "full_path": "keyword",
    "object_type": "keyword",
    "name": "keyword",
    "document": "text",
}

# Параметры scalar-квантизации — общие для создания и warm-up PATCH
# (fa9: set_always_ram обязан воспроизводить тот же конфиг, иначе PATCH
# перезапишет квантизацию с другим quantile)
_QUANT_TYPE = "int8"
_QUANT_QUANTILE = 0.99


def to_qdrant_id(collection_name: str, chunk_id: str) -> str:
    """Детерминированный UUID точки из исходного строкового ID чанка."""
    return str(uuid.uuid5(_NS, f"{collection_name}:{chunk_id}"))


def _where_to_filter(where: dict[str, Any] | None) -> Any | None:
    """Простой where -> qdrant Filter: {k: v}, {k: {$eq: v}}, {$and: [...]}."""
    from qdrant_client import models

    if not where:
        return None
    must = []
    for key, cond in where.items():
        if key == "$and":
            sub = [_where_to_filter(s) for s in (cond if isinstance(cond, list) else [cond])]
            sub = [f for f in sub if f is not None]
            if sub:
                must.append(
                    models.Filter(
                        must=[c for f in sub for c in (f.must or [])],
                        must_not=[c for f in sub for c in (f.must_not or [])],
                    )
                )
        elif isinstance(cond, dict):
            for op, val in cond.items():
                if op == "$eq":
                    must.append(models.FieldCondition(key=key, match=models.MatchValue(value=val)))
        else:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=cond)))
    return models.Filter(must=must) if must else None


class VectorCollection:
    """Одна коллекция с duck-интерфейсом, привычным нашему фасаду."""

    def __init__(self, client: Any, name: str, on_disk: bool = False):
        self.name = name
        self._client = client
        self._on_disk = on_disk
        self._exists: bool | None = None

    def _ensure(self, dim: int) -> None:
        """Ленивое создание коллекции по первой реальной размерности.

        Новые коллекции создаются с int8-квантизацией (R1, analysis-
        semantic §6); fa9: RAM-копия по умолчанию НЕ держится
        (always_ram=False — все коллекции on_disk, RAM не едят); прогрев
        always_ram=True ставит только активной конфигурации warm-up при
        config_use. Env SEMANTIC_QUANTIZATION=off выключает квантизацию
        вовсе; SEMANTIC_WARM=off — exotic-режим: всем созданным снова
        always_ram=True (прежнее поведение sem-R1)."""
        from qdrant_client import models

        if self._exists:
            return
        if not self._client.collection_exists(self.name):
            if dim <= 0:
                return
            kwargs: dict[str, Any] = {
                "collection_name": self.name,
                "vectors_config": models.VectorParams(
                    size=dim, distance=models.Distance.COSINE,
                    on_disk=self._on_disk),
            }
            if os.getenv("SEMANTIC_QUANTIZATION", "").strip().lower() != "off":
                always_ram = (os.getenv("SEMANTIC_WARM", "").strip().lower()
                              == "off")
                kwargs["quantization_config"] = models.ScalarQuantization(
                    scalar=models.ScalarQuantizationConfig(
                        type=models.ScalarType.INT8,
                        quantile=_QUANT_QUANTILE, always_ram=always_ram))
            self._client.create_collection(**kwargs)
            for field, ftype in _PAYLOAD_INDEXES.items():
                try:
                    self._client.create_payload_index(self.name, field, ftype)
                except Exception:
                    pass  # индекс — ускорение, не критично
        self._exists = True

    def count(self) -> int:
        """Точное число точек; исключения НЕ глотаются — упавший сервер
        должен отличаться от пустой коллекции."""
        if self._exists is False:
            return 0
        if not self._client.collection_exists(self.name):
            self._exists = False
            return 0
        return self._client.count(self.name, exact=True).count

    def dimension(self) -> int:
        """Размерность векторов коллекции (0 — не определена/сбой)."""
        try:
            info = self._client.get_collection(self.name)
            size = info.config.params.vectors.size
            return int(size) if size else 0
        except Exception:
            return 0

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        """Идемпотентная запись: стабильные ID перезаписывают точки."""
        from qdrant_client import models

        if not ids:
            return
        self._ensure(dim=len(embeddings[0]))
        points = [
            models.PointStruct(
                id=to_qdrant_id(self.name, cid),
                vector=vec,
                payload={**{k: v for k, v in meta.items() if v is not None},
                         "document": doc, "chroma_id": cid},
            )
            for cid, doc, meta, vec in zip(ids, documents, metadatas, embeddings)
        ]
        for i in range(0, len(points), _UPSERT_BATCH):
            self._client.upsert(self.name, points=points[i : i + _UPSERT_BATCH], wait=True)

    def query(
        self,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
        where_document: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ближайшие точки. Один вектор на вызов; where_document=$contains —
        текстовый фильтр по payload.document."""
        from qdrant_client import models

        if self._exists is False:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        if not query_embeddings:
            raise ValueError("query_embeddings обязателен")
        if len(query_embeddings) > 1:
            raise ValueError("один вектор на вызов, получен батч")
        self._ensure(0)
        flt = _where_to_filter(where)
        if where_document and "$contains" in where_document:
            text_cond = models.FieldCondition(
                key="document", match=models.MatchText(text=where_document["$contains"])
            )
            flt = models.Filter(must=[*(flt.must if flt else []), text_cond])
        # compass-lsx / операция 7vn (замеры 2026-11-15): исходные векторы
        # on_disk, квантованная копия в RAM — rescoring тянет исходники с
        # диска (2–7 с на запрос); без него качество не теряется (golden-
        # set операции). rescore=False всегда: коллекции без квантизации
        # параметр просто игнорируют.
        res = self._client.query_points(
            self.name, query=query_embeddings[0], limit=n_results,
            query_filter=flt, with_payload=True,
            search_params=models.SearchParams(
                quantization=models.QuantizationSearchParams(rescore=False)),
        )
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict[str, Any]] = []
        dists: list[float] = []
        for p in res.points:
            payload = dict(p.payload or {})
            docs.append(payload.pop("document", ""))
            ids.append(str(payload.pop("chroma_id", p.id)))
            metas.append(payload)
            dists.append(1.0 - float(p.score))  # cosine similarity -> distance
        return {
            "ids": [ids], "documents": [docs],
            "metadatas": [metas], "distances": [dists],
        }

    def get(self, limit: int = 100, offset: Any = None,
            scroll_batch: int = _SCROLL_BATCH) -> dict[str, Any]:
        """Перебор точек scroll-курсором: next_offset передавать следующим
        вызовом; None — коллекция исчерпана. Числовой offset зациклит.
        scroll_batch — размер одного scroll-запроса (R8/A10: 2048 в resume,
        590k точек ≈ 290 запросов вместо 2300 при дефолте 256)."""
        if self._exists is False:
            return {"ids": [], "documents": [], "metadatas": [], "next_offset": None}
        self._ensure(0)
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict[str, Any]] = []
        cursor = offset
        while len(ids) < limit:
            step = min(scroll_batch, limit - len(ids))
            batch, cursor = self._client.scroll(self.name, limit=step, offset=cursor)
            if not batch:
                cursor = None
                break
            for p in batch:
                payload = dict(p.payload or {})
                ids.append(str(payload.pop("chroma_id", p.id)))
                docs.append(payload.pop("document", ""))
                metas.append(payload)
            if cursor is None or len(batch) < step:
                break
        return {"ids": ids, "documents": docs, "metadatas": metas, "next_offset": cursor}

    def scroll_ids(self, limit: int = 100, offset: Any = None,
                   scroll_batch: int = _SCROLL_BATCH) -> tuple[list[str], Any]:
        """Только ID точек (qdrant-UUID), без payload. PERF-5/compass-ehr:
        resume-перебор сравнивал множества ID, но через get() тянул полные
        payload'ы (тексты — сотни МБ на 590k точек). Курсор как в get():
        next_offset передавать следующим вызовом, None — исчерпана.
        Старый get() не тронут (поиск по payload и т.п. остаются на нём)."""
        if self._exists is False:
            return [], None
        self._ensure(0)
        ids: list[str] = []
        cursor = offset
        while len(ids) < limit:
            step = min(scroll_batch, limit - len(ids))
            batch, cursor = self._client.scroll(self.name, limit=step,
                                                offset=cursor, with_payload=False)
            if not batch:
                cursor = None
                break
            ids.extend(str(p.id) for p in batch)
            if cursor is None or len(batch) < step:
                break
        return ids, cursor

    def delete(self, where: dict[str, Any] | None = None, ids: list[str] | None = None) -> None:
        """Удаление по исходным ID или по фильтру."""
        from qdrant_client import models

        if ids:
            qids = [to_qdrant_id(self.name, c) for c in ids]
            self._client.delete(self.name, points_selector=models.PointIdsList(points=qids))
            return
        flt = _where_to_filter(where)
        if flt is None:
            return
        self._client.delete(self.name, points_selector=models.FilterSelector(filter=flt))


class VectorStore:
    """Фабрика коллекций: серверный Qdrant (QDRANT_URL) или embedded (путь)."""

    def __init__(self, path: str | None = None, url: str | None = None, on_disk: bool = False):
        from qdrant_client import QdrantClient

        self.on_disk = on_disk
        if url:
            self.mode = "server"
            self.client = QdrantClient(url=url)
        else:
            self.mode = "local"
            self.client = QdrantClient(path=str(path or ":memory:"))

    def collection(self, name: str) -> VectorCollection:
        return VectorCollection(self.client, name, on_disk=self.on_disk and self.mode == "server")

    def delete_prefixed_code(self, prefix: str = "cfg_") -> list[str]:
        """Удалить все коллекции векторов вида <prefix>*_code.

        Смена эмбеддинг-модели: старые векторы несовместимы с новой
        моделью (и chunk_id не содержит модель — resume их пропустит),
        поэтому коллекции пересоздаются с нуля. Возвращает имена
        удалённых; ошибки Qdrant — наружу (вызывающий решает, критично
        ли это: best-effort удаление не должно блокировать сохранение).
        """
        deleted: list[str] = []
        for coll in self.client.get_collections().collections:
            name = coll.name
            if name.startswith(prefix) and name.endswith("_code"):
                self.client.delete_collection(name)
                deleted.append(name)
        return deleted


def set_always_ram(client: Any, collection: str, flag: bool,
                   *, force: bool = False) -> dict:
    """fa9: PATCH quantization scalar int8 always_ram существующей
    коллекции (update_collection) — выгрузка RAM-копии (False) или прогрев
    (True). Конфиг воспроизводит создание (_QUANT_QUANTILE), иначе PATCH
    перезаписал бы квантизацию с другим quantile. Честные skips: коллекции
    нет; scalar int8-квантизации нет (создавать PATCH'ем = пересборка —
    не здесь); флаг уже такой (no-op, важно для повторных прогревов).

    force=True (compass-zi2) обходит no-op пропуск «флаг уже такой»:
    после рестарта qdrant конфиг always_ram=True персистит, а физическая
    int8-копия в RAM холодная — повторный enable (лечение инцидента
    17.11) заставляет оптимизатор поднять квантование заново.
    Возвращает {"updated": ..., "collection": ...} | {"skipped": ...};
    при force в updated-ответе есть метка "forced": True.
    """
    from qdrant_client import models

    try:
        info = client.get_collection(collection)
    except Exception as exc:
        return {"error": f"коллекция {collection} недоступна: "
                         f"{type(exc).__name__}: {exc}"}
    quant = getattr(getattr(info.config, "quantization_config", None),
                    "scalar", None)
    if quant is None:
        return {"skipped": f"у {collection} нет scalar-квантизации — "
                           "PATCH её не создаёт (пересборка вне этой "
                           "операции)"}
    if bool(quant.always_ram) == flag and not force:
        return {"skipped": f"{collection}: always_ram уже {flag}"}
    client.update_collection(
        collection_name=collection,
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8, quantile=_QUANT_QUANTILE,
                always_ram=flag)))
    if force:
        return {"updated": flag, "collection": collection, "forced": True}
    return {"updated": flag, "collection": collection}
