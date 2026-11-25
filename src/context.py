# -*- coding: utf-8 -*-
"""Контекст чата агента: X-Chat-Name → активная конфигурация.

Механика (согласована с владельцем, #32):
- каждый агентский чат передаёт заголовок X-Chat-Name;
- привязка чат → конфигурация хранится персистентно в
  /workspace/Settings/sessions.json — переживает рестарты;
- тулы без явного config читают конфигурацию своей сессии;
- сессий-объектов нет: заголовок — стабильный ключ, «Session not
  found» в этой архитектуре не возникает.
"""
from __future__ import annotations

import contextvars

from . import workspace

# Заголовок, устанавливаемый ASGI-мiddleware на каждый HTTP-запрос
chat_name_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "chat_name", default="panel")

PANEL_CONTEXT = "panel"
DEFAULT_CONTEXT = "default"


def current_context_name() -> str:
    """Имя текущего контекста: чат агента, панель или default."""
    return chat_name_var.get() or DEFAULT_CONTEXT


def active_config_id() -> str | None:
    """ID активной конфигурации текущего контекста.

    Приоритет: привязка контекста из sessions.json →
    default_config_id из settings.json → None (не выбрана).
    """
    chat = current_context_name()
    sessions = workspace.load_sessions().get("sessions") or {}
    binding = sessions.get(chat)
    if binding and binding.get("config_id"):
        return binding["config_id"]
    return workspace.load_settings().get("default_config_id")


def bind_context(config_id: str, chat: str | None = None) -> dict:
    """Привязать конфигурацию к контексту (чат агента или панель).

    Запись обновляется точечно (через workspace.update_sessions): поля
    активности last_seen/requests от session_activity не затираются.
    """
    chat = chat or current_context_name()
    prev: dict = {}

    def _bind(data: dict) -> None:
        sessions = data.setdefault("sessions", {})
        record = sessions.get(chat) or {}
        prev["config_id"] = record.get("config_id")
        record["config_id"] = config_id
        record["updated_at"] = workspace.datetime_utc()
        sessions[chat] = record

    workspace.update_sessions(_bind)
    return {"context": chat, "config_id": config_id,
            "previous": prev.get("config_id")}


def bind_default(config_id: str) -> None:
    """Сменить дефолт (без чат-заголовка) — панель при активации.

    Привязка панели к КОНФИГУРАЦИИ: bind_context(config_id, PANEL_CONTEXT)
    — сигнатура (config_id, chat); инверсия писала config_id='panel'
    (F1/compass-zjp)."""
    settings = workspace.load_settings()
    settings["default_config_id"] = config_id
    workspace.save_settings(settings)
    bind_context(config_id, PANEL_CONTEXT)
