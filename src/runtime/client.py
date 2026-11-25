# -*- coding: utf-8 -*-
"""Клиент рантайм-блока: транспорт ядро → база 1С (.epf CompassRuntime).

Р1: транспорт — HTTP JSON к эндпоинту, который поднимает нативная
компонента внутри сеанса 1С (по образцу 1c-mcp-toolkit; наш модуль —
своя реализация, транспорт подключается конфигурацией RUNTIME_URL).
Пока эндпоинт не настроен — тулы честно сообщают «контур не подключён».
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class RuntimeUnavailable(RuntimeError):
    """Рантайм-контур не подключён или база не ответила."""


class RuntimeClient:
    """Тонкий клиент песочницы (POST /sandbox, JSON)."""

    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: float | None = None):
        self.url = (url or os.getenv("RUNTIME_URL", "")).rstrip("/")
        self.token = token or os.getenv("RUNTIME_TOKEN", "")
        self.timeout = float(timeout or os.getenv("RUNTIME_TIMEOUT", "120"))

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Вызов метода рантайма; исключения — наружу с честной причиной."""
        if not self.configured:
            raise RuntimeUnavailable(
                "RUNTIME_URL не задан: рантайм-контур (база 1С с обработкой "
                "CompassRuntime) не подключён")
        body = json.dumps({"method": method, "params": payload},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Runtime-Token"] = self.token
        req = urllib.request.Request(self.url + "/" + method, data=body,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeUnavailable(f"база ответила HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeUnavailable(f"база недоступна: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeUnavailable("неожиданный формат ответа рантайма")
        return data

    def sandbox_run(self, code: str, policy: str = "strict",
                    timeout_s: int = 30) -> dict[str, Any]:
        """Песочница выполнения кода — ОТЛОЖЕНО решением владельца (#32):
        рантайм сфокусирован на разведке данными. Метод оставлен для
        будущего возвращения sandbox-контура.
        """
        return self.call("sandbox", {"code": code, "policy": policy,
                                     "timeout_s": timeout_s})

    def ping(self) -> dict[str, Any]:
        """Проверка живости контура (версия обработки, база, режим)."""
        return self.call("ping", {})

    # ------------------------------------------- разведка данными (Р1, #32)

    def data_query(self, query: str, params: dict | None = None,
                   limit: int = 10) -> dict[str, Any]:
        """SELECT-запрос к живой базе (принудительные ПЕРВЫЕ N, read-only)."""
        return self.call("data_query", {"query": query,
                                        "params": params or {}, "limit": limit})

    def get_object(self, type_name: str, name: str, key: str,
                   key_kind: str = "код") -> dict[str, Any]:
        """Объект по ключу (код/наименование/гуид): реквизиты + табличные части."""
        return self.call("get_object", {"type": type_name, "name": name,
                                        "key": key, "key_kind": key_kind})

    def register_records(self, register_name: str, limit: int = 10) -> dict[str, Any]:
        """Записи регистра (проводки/остатки/сведения) — срез первых N."""
        return self.call("register_records",
                         {"register": register_name, "limit": limit})

    def catalog_samples(self, catalog_name: str, limit: int = 5) -> dict[str, Any]:
        """Примеры заполнения элементов справочника (первые N с реквизитами)."""
        return self.call("catalog_samples",
                         {"catalog": catalog_name, "limit": limit})

    def records(self, object_name: str, fields: list | None = None,
                filters: list | None = None, order: list | None = None,
                limit: int = 100, cursor: str = "",
                virtual: str = "", vt_params: dict | None = None) -> dict[str, Any]:
        """Универсальная выборка записей объекта («Вид.Имя», keyset-курсор).

        Ключи протокола (диспетчер E1, arch §2): object, fields, filters,
        order, limit, cursor, virtual, vt_params; внутренние структуры
        отборов/порядка — по докстрингу ЗаписиОбъекта (E3):
        {Поле, Операция, Значение} / {Поле, Направление}.
        """
        return self.call("records", {"object": object_name,
                                     "fields": fields or [],
                                     "filters": filters or [],
                                     "order": order or [],
                                     "limit": limit,
                                     "cursor": cursor,
                                     "virtual": virtual,
                                     "vt_params": vt_params or {}})

    def document_movements(self, type_name: str, name: str, key: str,
                           key_kind: str = "код",
                           limit_per_register: int = 10) -> dict[str, Any]:
        """Движения одного документа по всем регистрам (только непустые)."""
        return self.call("document_movements",
                         {"type": type_name, "name": name, "key": key,
                          "key_kind": key_kind,
                          "limit_per_register": limit_per_register})

    def event_log(self, start: str, end: str, object_guid: str = "",
                  user: str = "", event: str = "", limit: int = 100) -> dict[str, Any]:
        """Записи журнала регистрации за период (arch §13).

        Даты — строки ГГГГММДДЧЧММСС; object_guid — ГУИД объекта или "".
        """
        return self.call("event_log", {"start": start, "end": end,
                                       "object": object_guid, "user": user,
                                       "event": event, "limit": limit})


_client: RuntimeClient | None = None


def runtime_client() -> RuntimeClient:
    """Ленивый singleton клиента рантайма."""
    global _client
    if _client is None:
        _client = RuntimeClient()
    return _client
