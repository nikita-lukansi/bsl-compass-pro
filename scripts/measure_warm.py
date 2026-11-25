# -*- coding: utf-8 -*-
"""fa9: контрольные замеры always_ram-переключения на живом Qdrant.

Методика (записать вывод в bd-комментарий compass-fa9):
1. длительность update_collection(always_ram=False) — API-подтверждение
   выгрузки RAM-копии;
2. длительность update_collection(always_ram=True) — API-подтверждение
   прогрева (если Qdrant запускает пересборку квантованных — время
   вырастет, это и фиксируем; вариант B со снапшотами решать владельцу);
3. латентность query_points сразу после прогрева (холодный путь on_disk)
   и серия после (тёплый путь из RAM);
4. размер коллекции/точек для соотнесения (ГБ, точки).
Начальное состояние always_ram возвращается по завершении (полигон не
меняется). Запуск на хосте с доступом к Qdrant:

  python scripts/measure_warm.py --url http://qdrant:6333 \
      --collection cfg_Compass_KA_798247c2_code --repeats 3

Qdrant недоступен из окружения coder-b2 (нет маршрута к полигону) —
замер выполняет владелец/b1 этим скриптом.
"""
from __future__ import annotations

import argparse
import json
import time


def _quant(flag: bool):
    from qdrant_client import models

    from src.semantic.vector_store import _QUANT_QUANTILE
    return models.ScalarQuantization(
        scalar=models.ScalarQuantizationConfig(
            type=models.ScalarType.INT8, quantile=_QUANT_QUANTILE,
            always_ram=flag))


def _timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    res = fn(*args, **kwargs)
    return round(time.perf_counter() - t0, 2), res


def measure(url: str, collection: str, repeats: int) -> dict:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=url)
    info = client.get_collection(collection)
    initial_ram = bool(info.config.quantization_config
                       and info.config.quantization_config.scalar
                       and info.config.quantization_config.scalar.always_ram)
    dim = int(info.config.params.vectors.size or 0)
    out = {"collection": collection,
           "points": client.count(collection, exact=True).count,
           "dim": dim,
           "initial_always_ram": initial_ram,
           # размер на диске API-модель не отдаёт — снимайте с volume
           # (du /data/qdrant/collections/<коллекция>) вручную
           }

    timings: dict = {}
    timings["evict_false_sec"], _ = _timed(
        client.update_collection, collection_name=collection,
        quantization_config=_quant(False))
    timings["warm_true_sec"], _ = _timed(
        client.update_collection, collection_name=collection,
        quantization_config=_quant(True))

    vec = [0.0] * dim if dim else [0.0]
    from qdrant_client import models
    cold: list = []
    for _ in range(repeats):
        t, _ = _timed(client.query_points, collection, query=vec, limit=5,
                      search_params=models.SearchParams(
                          quantization=models.QuantizationSearchParams(
                              rescore=False)))
        cold.append(t)
    timings["query_after_warm_sec"] = cold

    if not initial_ram:  # исходно холодная: снять прогрев (полигон не меняется)
        timings["restore_sec"], _ = _timed(
            client.update_collection, collection_name=collection,
            quantization_config=_quant(False))
    out["timings_sec"] = timings
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="fa9: замеры always_ram-переключения коллекции")
    ap.add_argument("--url", required=True, help="Qdrant URL (qdrant:6333)")
    ap.add_argument("--collection", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    report = measure(args.url, args.collection, args.repeats)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
