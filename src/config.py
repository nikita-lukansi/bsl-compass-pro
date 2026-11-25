# -*- coding: utf-8 -*-
"""Конфигурация ядра Pro: системный уровень (env) + активная конфигурация.

Системное (порт, токен, эмбеддинг-провайдер) — env. Конфигурационное
(активный дамп/индекс/алиас) — src.state (bootstrap через панель);
env COMPASS_* остаётся приоритетнее для режима разработчика.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import state

APP_VERSION = "0.1.0-proto.2"


def git_sha() -> str:
    """SHA коммита, из которого собран образ (env COMPASS_GIT_SHA).

    Bake при docker build: ARG GIT_SHA (Dockerfile) ← compose build.args
    ← env команды `GIT_SHA=$(git rev-parse --short HEAD) docker compose
    build`. Вне образа/без bake — "dev" (1mj.7: защита от «старый
    контейнер» — видимое отличие собранного кода).
    """
    return os.getenv("COMPASS_GIT_SHA") or "dev"


@dataclass
class Config:
    """Системные настройки; активная конфигурация — через state."""

    port: int = int(os.getenv("PORT", "8240"))
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    # Семантика (необязательный слой)
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "")
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "")
    qdrant_url: str = os.getenv("QDRANT_URL", "")

    @property
    def index_db(self):
        """Активный индекс ТЕКУЩЕГО КОНТЕКСТА (реестр, env — fallback)."""
        env = os.getenv("COMPASS_INDEX_DB")
        if env:
            return Path(env)
        try:
            from .facade.tools import active_index_db
            return active_index_db()
        except Exception:
            return None

    @property
    def source_root(self):
        env = os.getenv("COMPASS_SOURCE_ROOT")
        if env:
            return Path(env)
        try:
            cid = self.active_config_id
            if cid:
                from .registry import get_config
                entry = get_config(cid)
                if entry:
                    return Path(entry["source_root"])
        except Exception:
            pass
        return state.active_source_root()

    @property
    def active_config_id(self):
        try:
            from .context import active_config_id
            return active_config_id()
        except Exception:
            return None

    @property
    def alias(self) -> str:
        """Отображаемое имя активной конфигурации (для панели/статусов)."""
        cid = self.active_config_id
        if cid:
            try:
                from .registry import get_config
                entry = get_config(cid)
                if entry:
                    return entry.get("display_name") or cid
            except Exception:
                pass
        return state.active_alias()

    @property
    def index_ready(self) -> bool:
        db = self.index_db
        return db is not None and db.exists()

    def embedding_settings(self) -> dict:
        """Эффективные параметры эмбеддингов: settings.json["embedding"]
        переопределяет env-дефолты. Одна модель на все конфигурации.

        Возвращает {base_url, model, api_key, source}; source — «settings»,
        если из файла пришла хотя бы модель или base_url (api_key сам по себе
        источником не считается). Пустой api_key из файла = брать из env.
        """
        override: dict = {}
        try:
            from . import workspace
            override = workspace.load_settings().get("embedding") or {}
        except Exception:
            override = {}
        model = str(override.get("model") or self.embedding_model or "").strip()
        base_url = str(override.get("base_url") or self.embedding_base_url or "").strip()
        api_key = override.get("api_key") or self.embedding_api_key or ""
        return {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "source": "settings" if (override.get("model") or override.get("base_url"))
                      else "env",
        }

    @property
    def semantic_ready(self) -> bool:
        """Семантика возможна: провайдер задан и модель выбрана."""
        emb = self.embedding_settings()
        return bool(emb["base_url"] and emb["model"])

    def its_credentials(self) -> dict:
        """Креды ИТС для выгрузки документации: env ITS_* + override
        settings.json["its"] (login/password/cookie/delay).

        Наружу (статусы, ответы тулов) отдаётся только поле source —
        «settings»/«env»/«none»; login/password/cookie не логируются и в
        параметры тулов не попадают (спецификация §3). «none» — только
        публичные страницы, обновление корпуса недоступно.
        """
        override: dict = {}
        try:
            from . import workspace
            override = workspace.load_settings().get("its") or {}
        except Exception:
            override = {}
        login = str(override.get("login") or os.getenv("ITS_LOGIN", "")).strip()
        password = str(override.get("password") or os.getenv("ITS_PASSWORD", ""))
        cookie = str(override.get("cookie") or os.getenv("ITS_COOKIE", "")).strip()
        try:
            delay = float(override.get("delay") or os.getenv("ITS_FETCH_DELAY", "2.5"))
        except (TypeError, ValueError):
            delay = 2.5
        if override.get("login") or override.get("cookie"):
            source = "settings"
        elif os.getenv("ITS_LOGIN") or os.getenv("ITS_COOKIE"):
            source = "env"
        else:
            source = "none"
        return {"login": login, "password": password, "cookie": cookie,
                "delay": delay, "source": source}


config = Config()
