# -*- coding: utf-8 -*-
"""Офлайн-режим рантайм-блока: каталоги обмена, атомарная запись, статус.

Пакетный конвейер (Т2, arch §3/§4): файловый протокол queue/req_<id>.json
→ results/resp_<id>.json → archive/ ведёт реестр задач (tasks.py) со
своим watcher'ом; здесь — только слой каталогов (ensure_dirs), атомарная
запись (write_atomic) и сводка-счётчики для панели/runtime_status.
Деанонимизация токенов для агента — deanonymize().
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .anonymizer import get_anonymizer

# S3: системная зона /data/system/offline (env COMPASS_OFFLINE_PATH — тесты/хост)
OFFLINE_ROOT = Path(os.getenv("COMPASS_OFFLINE_PATH", "/data/system/offline"))
QUEUE_DIR = "queue"      # пакетные req (arch §3): пишет компас, забирает 1С
RESULTS_DIR = "results"  # пакетные resp (arch §3): пишет 1С, забирает watcher


def ensure_dirs(root: Path | None = None) -> None:
    """Каталоги обмена queue/ results/ archive/ существуют (идемпотентно).

    root=None → OFFLINE_ROOT; реестр задач передаёт свой root.
    """
    base = Path(root) if root else OFFLINE_ROOT
    for sub in (QUEUE_DIR, RESULTS_DIR, "archive"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def write_atomic(path: Path, text: str) -> None:
    """Атомарная запись: tmp с уникальным суффиксом → os.replace.

    Ватчер читает только целые файлы — полузаписи исключены.
    """
    import uuid
    tmp = path.with_suffix(path.suffix + f".tmp{uuid.uuid4().hex[:6]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def deanonymize(text_or_data: Any) -> Any:
    """Токены → исходные значения (для кода агента, ответов пользователю)."""
    anonymizer = get_anonymizer()
    if isinstance(text_or_data, str):
        return anonymizer.deanonymize_text(text_or_data)
    return anonymizer.deanonymize_data(text_or_data)


def offline_status() -> dict:
    """Состояние офлайн-конвейера: счётчики реестра задач (arch §9) +
    бейдж версии рантайма base {name, version} из последнего resp
    (compass-5vq; None — обменов ещё не было).

    Офлайн выключен → {"info": ...}: реестр/watcher не создаются зря,
    каталоги-заглушки в online-режиме не нужны.
    """
    if os.getenv("RUNTIME_MODE", "online").lower() != "offline":
        return {"info": "офлайн-режим выключен (RUNTIME_MODE=online)"}
    from .tasks import get_registry  # лениво: tasks импортирует offline
    reg = get_registry()
    counts: dict[str, int] = {}
    for t in reg.list():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "pending_requests": counts.get("queued", 0) + counts.get("in_1c", 0),
        "ready_responses": counts.get("ready", 0),
        "statuses": counts,
        "base": reg.runtime_base(),
    }
