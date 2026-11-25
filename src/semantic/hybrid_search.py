# -*- coding: utf-8 -*-
"""Гибридный поиск: вектор + буквальный fulltext через RRF (clean-room).

Прошлая реализация Community портировалась из недоступного репозитория
(comol/1c_code_metadata_mcp — происхождение непроверяемо), поэтому здесь
логика написана с нуля по стандартной формуле Reciprocal Rank Fusion.
Объём маленький: честнее переписать, чем тянуть непроверяемое происхождение.
"""
from __future__ import annotations

import os
from typing import Any

from .embeddings_client import EmbeddingsClient, split_camel_case

_QUERY_INSTRUCT_DEFAULT = (
    "Instruct: по поисковому запросу найди релевантные функции, процедуры "
    "и объекты в коде и метаданных 1С\nQuery: "
)


def rrf_fuse(
    ranked_lists: list[list[str]],
    k: int = 60,
    top_k: int | None = None,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: каждый ID вносит 1/(k + rank) из каждого списка.

    Стандартный комбинатор разнородных ранжирований (FTS + вектор) без
    необходимости сравнимых скоров. Возвращает [(id, score)] по убыванию.
    """
    scores: dict[str, float] = {}
    for results in ranked_lists:
        for rank, ident in enumerate(results):
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank)
    fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return fused[:top_k] if top_k else fused


class HybridSearch:
    """Поиск по одной коллекции: векторная ветка всегда, буквальная — для
    однословных запросов (точные идентификаторы), слияние через RRF.

    Провайдер эмбеддингов передаётся снаружи (embed_query), коллекция —
    duck-интерфейсом VectorCollection, так что слой тестируется на моках.
    """

    def __init__(self, collection: Any, embeddings: EmbeddingsClient):
        self.collection = collection
        self.embeddings = embeddings

    def _query_instruct(self) -> str:
        """Instruct-префикс запроса (qwen3-embedding рассчитан на него).

        EMBEDDING_QUERY_INSTRUCT: не задано или on/1/true/yes — дефолтная
        инструкция (compose-дефолт «on» раньше приклеивался к запросу
        буквальным словом, P1-6); ""/0/off/none — выключить; иное значение —
        дословно (своя инструкция)."""
        val = os.getenv("EMBEDDING_QUERY_INSTRUCT")
        if val is None:
            return _QUERY_INSTRUCT_DEFAULT
        normalized = val.strip().lower()
        if normalized in ("", "0", "off", "none"):
            return ""
        if normalized in ("on", "1", "true", "yes"):
            return _QUERY_INSTRUCT_DEFAULT
        return val

    def _embed_input(self, query: str) -> str:
        """Текст для эмбеддинга: instruct + camelCase-разбиение."""
        prepared = split_camel_case(query)
        instruct = self._query_instruct()
        return instruct + prepared if instruct else prepared

    def search(
        self,
        query: str,
        limit: int = 10,
        where: dict[str, Any] | None = None,
        fuse_fulltext: bool | None = None,
        query_timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Поиск: вектор всегда; буквальный $contains для однословных (или
        принудительно fuse_fulltext=True). Результат: метаданные + скор,
        нормированный к лучшему (агенту нужен осмысленный ранг).

        query_timeout — таймаут эмбеддинга запроса (F8/xsv: запросный путь
        короче индексационного)."""
        single_word = len(query.split()) == 1
        use_fulltext = fuse_fulltext if fuse_fulltext is not None else single_word

        vector = self.embeddings.embed_query(self._embed_input(query),
                                             timeout=query_timeout)
        vec_res = self.collection.query(
            query_embeddings=[vector], n_results=limit, where=where
        )
        vec_ids = vec_res["ids"][0]
        if not use_fulltext or not vec_ids:
            return self._format(vec_res, vec_ids, limit, base="vector")

        ft_res = self.collection.query(
            query_embeddings=[vector], n_results=limit,
            where=where, where_document={"$contains": query},
        )
        fused = rrf_fuse([vec_ids, ft_res["ids"][0]], top_k=limit)
        index = {cid: i for i, cid in enumerate(vec_ids)}
        best = fused[0][1] or 1.0
        out = []
        for cid, score in fused:
            i = index.get(cid)
            if i is None:
                continue
            row = self._row(vec_res, i)
            row["score"] = round(score / best, 3)
            out.append(row)
        return out

    @staticmethod
    def _row(res: dict[str, Any], i: int) -> dict[str, Any]:
        meta = res["metadatas"][0][i]
        distance = res["distances"][0][i]
        return {
            "id": res["ids"][0][i],
            "content": res["documents"][0][i],
            "metadata": meta,
            "full_path": meta.get("full_path", ""),
            "name": meta.get("name", ""),
            # 1/(1+d) монотонен при любой метрике, осмыслен для чтения
            "distance_score": round(1.0 / (1.0 + distance), 3) if distance is not None else 0.0,
        }

    def _format(self, res, ids, limit, base):
        rows = [self._row(res, i) for i in range(min(len(ids), limit))]
        # score, нормированный к лучшему — единая семантика с RRF-путём
        if rows:
            best = max(r["distance_score"] for r in rows) or 1.0
            for r in rows:
                r["score"] = round(r["distance_score"] / best, 3)
        return rows
