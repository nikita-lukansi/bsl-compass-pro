# -*- coding: utf-8 -*-
"""Режим рантайма из env — ядро ядра, без зависимости от слоя рантайма
(аудит модульности §7 №1-2: eager-импорт S3 в фасад ядра разорван)."""
import os


def runtime_brief() -> dict:
    """Краткая справка режима: mode/configured/hint (без похода в слой)."""
    mode = os.getenv("RUNTIME_MODE", "online").lower()
    return {"mode": mode,
            "configured": bool(os.getenv("RUNTIME_URL", "")) or mode == "offline",
            "hint": "" if (os.getenv("RUNTIME_URL") or mode == "offline")
                    else "data_query недоступен: задайте RUNTIME_URL или "
                         "RUNTIME_MODE=offline"}
