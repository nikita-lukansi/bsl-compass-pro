# -*- coding: utf-8 -*-
"""Тулы реестра конфигураций (3.2): список и переключение контекста."""
from __future__ import annotations

from ..context import active_config_id, bind_context
from ..registry import all_configs, get_config
from ..runtime_mode import runtime_brief as _runtime_brief  # compass-c6z §7 №1-2
from . import tools


def reset_runtime_caches() -> None:
    """Сброс кэшей после смены конфигурации: читатель, семантика, repomap."""
    tools._reader = None
    tools._reader_db = None
    semantic = sys.modules.get(__name__.rsplit(".", 1)[0] + ".semantic_tools")
    if semantic is not None:
        # indexing НЕ трогаем (R13/A24): поток сам ставит False в finally;
        # сброс не должен разрешать вторую параллельную векторизацию
        semantic._sem.update(collection=None, store=None, embeddings=None)
    # P1-3a: рейтинг repomap привязан к индексу — после пересборки путь
    # может остаться прежним, содержимое уже новое; сброс обязателен
    from .repomap import reset_cache
    reset_cache()


import sys  # noqa: E402


def config_list() -> dict:
    """Реестр конфигураций: ID, имена, режим, векторы, активность контекста."""
    active_id = active_config_id()
    out = []
    for c in all_configs():
        source_version = c.get("source_version")
        out.append({
            "id": c["id"],
            "short_name": c.get("short_name", ""),
            "display_name": c.get("display_name", ""),
            "source_root": c.get("source_root", ""),
            "index_ready": Path(c["index_db"]).exists(),
            "mode": c.get("mode", "fast"),
            "vectors": (c.get("vectors") or {}).get("points", {}).get("code", 0),
            "active": c["id"] == active_id,
            # версия релиза и свежесть сборки (обратка 2026-11-15,
            # compass-4w7.4): config_info.Version читается из
            # Configuration.xml при добавлении; source_version — ПЕРВЫЕ
            # 8 символов sha256 (не полный хэш, экономия токенов);
            # отсутствующее поле — null, не ошибка
            "version": (c.get("config_info") or {}).get("Version"),
            "indexed_at": c.get("indexed_at"),
            "source_version": source_version[:8] if source_version else None,
        })
    # активная — первой (обратка 2026-11-15, compass-4w7.3); сортировка
    # стабильна: неактивные сохраняют прежний порядок all_configs
    out.sort(key=lambda c: not c["active"])
    return {"configs": out, "runtime": _runtime_brief()}


def config_use(config_id: str, wait_warm: bool = False) -> dict:
    """Переключить конфигурацию ТЕКУЩЕГО КОНТЕКСТА (чат агента/панель).

    wait_warm=true (compass-yho, обратка №5) — блокировать ответ до
    завершения прогрева семантики выбранной конфигурации: поллинг
    warm_state (semantic_tools.wait_warm — разумный таймаут, честная
    отмена); ожидание только когда прогрев реально запущен."""
    entry = get_config(config_id)
    if entry is None:
        known = [c["id"] for c in all_configs()]
        return {"error": f"конфигурация '{config_id}' не найдена", "known": known}
    binding = bind_context(config_id)
    reset_runtime_caches()
    out = {"active": config_id,
           "display_name": entry.get("display_name", ""),
           # compass-sm4: эхо режима семантики (контракт yho; config_list
           # уже несёт mode — тут паритет)
           "mode": entry.get("mode", "fast"),
           "previous": binding.get("previous")}
    # fa9: неблокирующий прогрев RAM-копии активной коллекции (выгрузка
    # прежней, прогрев новой); сбой/отказ семантики не ломает переключение
    try:
        from . import semantic_tools
        out["warm"] = semantic_tools.schedule_warmup(
            binding.get("previous"), config_id)
    except Exception as exc:  # семантика не настроена и т.п. — не ошибка смены
        out["warm"] = {"skipped": f"прогрев не выполнен: "
                                  f"{type(exc).__name__}: {exc}"}
        return out
    # compass-yho: ждём только идущий прогрев (skipped/info — не ждём)
    if wait_warm and isinstance(out.get("warm"), dict) \
            and out["warm"].get("state") == "warming":
        out["warm"].update(semantic_tools.wait_warm())
    return out


from pathlib import Path  # noqa: E402
