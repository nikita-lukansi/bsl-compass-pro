# -*- coding: utf-8 -*-
"""Активность сессий чатов: last_seen/requests для панели «Обзор».

Полноценных сессий-объектов в архитектуре нет (#32): заголовок X-Chat-Name —
стабильный ключ. Для таблицы «Последние сессии» храним только метрики:
  - память: {chat: {last_seen (epoch), requests}} — touch() на каждый
    значимый HTTP-запрос (из ChatNameMiddleware);
  - sessions.json: те же поля рядом с config_id (обратная совместимость —
    старые записи без них читаются как «без активности»). Запись в файл
    троттлится до раза в PERSIST_INTERVAL секунд на чат: панель опрашивает
    /api/status каждые 5 с, писать на каждый запрос незачем.

Потоки: touch зовётся в event-loop (middleware), запись — под общим локом
workspace.update_sessions вместе с context.bind_context; гонок записи нет.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import workspace

PERSIST_INTERVAL = 60.0  # сек между записями last_seen одного чата

_activity: dict[str, dict] = {}   # chat -> {last_seen: float, requests: int}
_persisted_at: dict[str, float] = {}  # chat -> epoch последней записи в файл
_lock = threading.Lock()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value) -> float:
    """ISO-строка → epoch; мусор → 0.0."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def touch(chat: str, now: float | None = None) -> None:
    """Отметить запрос чата: +1 запрос, свежий last_seen.

    Персистентность в sessions.json — не чаще PERSIST_INTERVAL секунд
    на чат (первая активность пишется всегда). Отказ IO не ломает запрос.
    """
    chat = (chat or "").strip()[:100] or "panel"
    ts = time.time() if now is None else now
    with _lock:
        entry = _activity.setdefault(chat, {"last_seen": 0.0, "requests": 0})
        entry["last_seen"] = ts
        entry["requests"] += 1
        need_persist = ts - _persisted_at.get(chat, 0.0) >= PERSIST_INTERVAL
        if need_persist:
            _persisted_at[chat] = ts
    if not need_persist:
        return

    last_seen_iso = _iso(ts)
    requests = entry["requests"]

    def _persist(data: dict) -> None:
        sessions = data.setdefault("sessions", {})
        rec = sessions.get(chat) or {}
        rec["last_seen"] = last_seen_iso
        rec["requests"] = requests
        sessions[chat] = rec

    try:
        workspace.update_sessions(_persist)
    except OSError:
        pass  # активность не должна ломать запрос


def restore() -> None:
    """При старте контейнера: поднять активность из sessions.json."""
    sessions = workspace.load_sessions().get("sessions") or {}
    with _lock:
        _activity.clear()
        _persisted_at.clear()
        for chat, rec in sessions.items():
            last_seen = _parse_iso((rec or {}).get("last_seen"))
            if not last_seen:
                continue
            _activity[chat] = {
                "last_seen": last_seen,
                "requests": int((rec or {}).get("requests") or 0),
            }
            # считаем persisted: не переписывать файл сразу после старта
            _persisted_at[chat] = last_seen


def snapshot() -> dict[str, dict]:
    """Копия in-memory реестра (для тестов и отладки)."""
    with _lock:
        return {chat: dict(rec) for chat, rec in _activity.items()}


def sessions_panel_list(limit: int = 12) -> list[dict]:
    """Строки для таблицы «Последние сессии» панели.

    Слияние трёх источников: привязки sessions.json, in-memory активность,
    имена конфигураций реестра. Сортировка по свежести (last_seen,
    фолбэк — updated_at), топ limit. last_seen — epoch (0 — неизвестно);
    requests — с момента старта контейнера или восстановленные из файла.
    """
    sessions = workspace.load_sessions().get("sessions") or {}
    registry = workspace.load_registry().get("configs") or {}
    activity = snapshot()

    rows: list[dict] = []
    for chat in set(sessions) | set(activity):
        binding = sessions.get(chat) or {}
        act = activity.get(chat) or {}
        config_id = binding.get("config_id")
        meta = registry.get(config_id) or {}
        last_seen = act.get("last_seen") or _parse_iso(binding.get("last_seen"))
        rows.append({
            "chat": chat,
            "config_id": config_id,
            "display_name": meta.get("display_name", ""),
            "short_name": meta.get("short_name", ""),
            "updated_at": binding.get("updated_at"),
            "last_seen": round(last_seen, 3),
            "requests": act.get("requests") or int(binding.get("requests") or 0),
        })
    rows.sort(key=lambda r: (r["last_seen"], _parse_iso(r.get("updated_at"))),
              reverse=True)
    return rows[:limit]
