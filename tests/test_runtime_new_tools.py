# -*- coding: utf-8 -*-
"""Новые рантайм-тулы E10: records, document_movements, event_log.

Проверяется маппинг имён методов протокола (диспетчер E1) и ключей
параметров, канонизация «Вид.Имя» в records (полная форма проходит
как есть — разбор на стороне обработки, E3). Герметично, без сети:
runtime_client подменяется monkeypatch'ем.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import runtime_tools  # noqa: E402
from src.runtime.client import (  # noqa: E402
    RuntimeClient,
    RuntimeUnavailable,
)


class _CaptureClient:
    """Мок рантайм-клиента: именованные методы поверх call(method, payload),
    как в реальном клиенте; фиксирует пары (метод, payload)."""

    configured = True

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer if answer is not None else {"Колонки": [],
                                                         "Строки": []}

    def call(self, method, payload):
        self.calls.append((method, payload))
        return self.answer

    def records(self, object_name, fields=None, filters=None, order=None,
                limit=100, cursor="", virtual="", vt_params=None):
        return self.call("records", {"object": object_name,
                                     "fields": fields or [],
                                     "filters": filters or [],
                                     "order": order or [],
                                     "limit": limit, "cursor": cursor,
                                     "virtual": virtual,
                                     "vt_params": vt_params or {}})

    def document_movements(self, type_name, name, key, key_kind="код",
                           limit_per_register=10):
        return self.call("document_movements",
                         {"type": type_name, "name": name, "key": key,
                          "key_kind": key_kind,
                          "limit_per_register": limit_per_register})

    def event_log(self, start, end, object_guid="", user="", event="",
                  limit=100):
        return self.call("event_log", {"start": start, "end": end,
                                       "object": object_guid, "user": user,
                                       "event": event, "limit": limit})


class _FailingClient:
    """Мок, поднимающий RuntimeUnavailable — честная ошибка в туле."""

    configured = True

    def call(self, method, payload):
        raise RuntimeUnavailable("RUNTIME_URL не задан: рантайм-контур "
                                 "(база 1С с обработкой CompassRuntime) "
                                 "не подключён")

    def records(self, *a, **kw):
        return self.call("records", {})

    def document_movements(self, *a, **kw):
        return self.call("document_movements", {})

    def event_log(self, *a, **kw):
        return self.call("event_log", {})


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setenv("RUNTIME_MODE", "online")
    cap = _CaptureClient()
    monkeypatch.setattr(runtime_tools, "runtime_client", lambda: cap)
    return cap


def test_records_mapping(capture):
    """records → метод «records» протокола; каноническая форма «Вид.Имя»
    (префикс вида) проходит как есть; все ключи E1 на месте."""
    out = runtime_tools.records(
        Объект="РегистрНакопления.Продажи",
        Поля=["Период", "Количество"],
        Отборы=[{"Поле": "Период", "Операция": "ge",
                 "Значение": "20260101000000"}],
        Порядок=[{"Поле": "Период", "Направление": "убыв"}],
        Лимит=200, Курсор="", Виртуальная="Обороты",
        ПараметрыВТ={"НачалоПериода": "20260101000000"})
    assert capture.calls == [("records", {
        "object": "РегистрНакопления.Продажи",  # префикс вида сохранён
        "fields": ["Период", "Количество"],
        "filters": [{"Поле": "Период", "Операция": "ge",
                     "Значение": "20260101000000"}],
        "order": [{"Поле": "Период", "Направление": "убыв"}],
        "limit": 200,
        "cursor": "",
        "virtual": "Обороты",
        "vt_params": {"НачалоПериода": "20260101000000"},
    })]
    assert out == {"Колонки": [], "Строки": []}


def test_records_defaults_and_passthrough(capture):
    """Дефолты E10 и честный пропуск неполной формы (ошибку отдаёт
    обработка — фасад не угадывает вид объекта)."""
    runtime_tools.records(Объект="Справочник.Контрагенты")
    method, payload = capture.calls[-1]
    assert method == "records"
    assert payload["object"] == "Справочник.Контрагенты"
    assert payload["limit"] == 100
    assert payload["cursor"] == ""
    assert payload["virtual"] == ""
    assert payload["fields"] == []
    assert payload["filters"] == []
    assert payload["order"] == []
    assert payload["vt_params"] == {}

    runtime_tools.records(Объект="Продажи")  # без вида — как есть
    assert capture.calls[-1][1]["object"] == "Продажи"


def test_document_movements_mapping(capture):
    """document_movements → ключи протокола type/name/key/key_kind/
    limit_per_register с дефолтами E10."""
    runtime_tools.document_movements("Документ", "РеализацияТоваровУслуг",
                                     "00-000001")
    assert capture.calls[-1] == ("document_movements", {
        "type": "Документ", "name": "РеализацияТоваровУслуг",
        "key": "00-000001", "key_kind": "код",
        "limit_per_register": 10,
    })
    runtime_tools.document_movements("Документ", "РеализацияТоваровУслуг",
                                     "00-000001", ВидКлюча="гуид",
                                     ЛимитНаРегистр=50)
    assert capture.calls[-1][1]["key_kind"] == "гуид"
    assert capture.calls[-1][1]["limit_per_register"] == 50


def test_event_log_mapping(capture):
    """event_log → ключи протокола start/end/object/user/event/limit."""
    runtime_tools.event_log("20261101000000", "20261112120000",
                            Объект="a1b2", Пользователь="Администратор",
                            Событие="Данные.Проведение", Лимит=500)
    assert capture.calls[-1] == ("event_log", {
        "start": "20261101000000", "end": "20261112120000",
        "object": "a1b2", "user": "Администратор",
        "event": "Данные.Проведение", "limit": 500,
    })


def test_offline_guard_suggests_submit(monkeypatch):
    """Офлайн-режим: единая подсказка на offline_submit, вызова клиента нет."""
    monkeypatch.setenv("RUNTIME_MODE", "offline")
    cap = _CaptureClient()
    monkeypatch.setattr(runtime_tools, "runtime_client", lambda: cap)
    for tool, args in (
            (runtime_tools.records, ("РегистрНакопления.Продажи",)),
            (runtime_tools.document_movements, ("Документ", "Реализация", "1")),
            (runtime_tools.event_log, ("20261101000000", "20261112120000"))):
        out = tool(*args)
        assert "info" in out and "offline_submit" in out["info"]
        assert out["method"] in ("records", "document_movements", "event_log")
    assert cap.calls == []


def test_unavailable_honest_error(monkeypatch):
    """Контур не подключён: тулы возвращают честную ошибку, не падают."""
    monkeypatch.setenv("RUNTIME_MODE", "online")
    monkeypatch.setattr(runtime_tools, "runtime_client",
                        lambda: _FailingClient())
    out = runtime_tools.records(Объект="Справочник.Контрагенты")
    assert "error" in out and "не подключён" in out["error"]
    out = runtime_tools.document_movements("Документ", "Реализация", "1")
    assert "error" in out
    out = runtime_tools.event_log("20261101000000", "20261112120000")
    assert "error" in out


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_client_wire_format(monkeypatch):
    """Клиентский слой: методы формируют POST /<метод> с ключами протокола."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"ok": True})

    monkeypatch.setattr("src.runtime.client.urllib.request.urlopen",
                        fake_urlopen)
    client = RuntimeClient(url="http://x")

    client.records("РегистрНакопления.Продажи", fields=["Период"],
                   limit=5, virtual="Остатки",
                   vt_params={"Период": "20260101000000"})
    assert captured["url"].endswith("/records")
    assert captured["body"]["method"] == "records"
    assert captured["body"]["params"]["object"] == "РегистрНакопления.Продажи"
    assert captured["body"]["params"]["fields"] == ["Период"]
    assert captured["body"]["params"]["limit"] == 5
    assert captured["body"]["params"]["virtual"] == "Остатки"
    assert captured["body"]["params"]["vt_params"] == {
        "Период": "20260101000000"}

    client.document_movements("Документ", "РеализацияТоваровУслуг", "00-1",
                              key_kind="наименование", limit_per_register=20)
    assert captured["url"].endswith("/document_movements")
    assert captured["body"]["params"]["key_kind"] == "наименование"
    assert captured["body"]["params"]["limit_per_register"] == 20

    client.event_log("20261101000000", "20261112120000", object_guid="g1",
                     event="Данные.Проведение", limit=7)
    assert captured["url"].endswith("/event_log")
    assert captured["body"]["params"]["start"] == "20261101000000"
    assert captured["body"]["params"]["object"] == "g1"
    assert captured["body"]["params"]["limit"] == 7
