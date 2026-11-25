# -*- coding: utf-8 -*-
"""Тесты рантайм-контура: клиент (транспорт, ошибки), тулы (гварды)."""
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


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_not_configured_honest(monkeypatch):
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    client = RuntimeClient()
    assert client.configured is False
    with pytest.raises(RuntimeUnavailable, match="RUNTIME_URL"):
        client.call("ping", {})
    # тулы разведки отвечают честной ошибкой, не падают
    res = runtime_tools.data_query("ВЫБРАТЬ 1")
    assert "error" in res and "не подключён" in res["error"]
    res = runtime_tools.catalog_samples("Контрагенты")
    assert "error" in res
    st = runtime_tools.runtime_status()
    assert st["configured"] is False and "hint" in st


def test_call_transport_and_token(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["token"] = req.headers.get("X-runtime-token")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"ok": True, "version": "Р1"})

    monkeypatch.setattr("src.runtime.client.urllib.request.urlopen",
                        fake_urlopen)
    client = RuntimeClient(url="http://127.0.0.1:9100/", token="S3")
    pong = client.ping()
    assert pong["version"] == "Р1"
    assert captured["url"] == "http://127.0.0.1:9100/ping"
    assert captured["token"] == "S3"
    client.sandbox_run("Результат = 1;", policy="strict")
    assert captured["url"].endswith("sandbox")
    assert captured["body"]["params"]["policy"] == "strict"


def test_transport_errors_wrapped(monkeypatch):
    import urllib.error

    def http_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr("src.runtime.client.urllib.request.urlopen", http_500)
    client = RuntimeClient(url="http://x")
    with pytest.raises(RuntimeUnavailable, match="HTTP 500"):
        client.ping()

    def dead(req, timeout=None):
        raise TimeoutError("timeout")

    monkeypatch.setattr("src.runtime.client.urllib.request.urlopen", dead)
    with pytest.raises(RuntimeUnavailable, match="недоступна"):
        client.ping()


def test_recon_tools_routing(monkeypatch):
    """Тулы разведки данными идут в правильные методы с параметрами."""
    captured = {}

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"rows": []})

    monkeypatch.setattr("src.runtime.client.urllib.request.urlopen", fake)
    monkeypatch.setenv("RUNTIME_URL", "http://x")
    from src.runtime import client as c
    c._client = None

    runtime_tools.data_query("ВЫБРАТЬ Ссылка ИЗ Справочник.Контрагенты",
                             limit=7, params={"К": "1"})
    assert captured["url"].endswith("data_query")
    assert captured["body"]["params"]["limit"] == 7
    assert captured["body"]["params"]["params"] == {"К": "1"}

    runtime_tools.get_object("Справочник", "Контрагенты", "00001",
                             key_kind="код")
    assert captured["url"].endswith("/get_object")
    assert captured["body"]["params"]["key_kind"] == "код"

    runtime_tools.register_records("Продажи", limit=3)
    assert captured["url"].endswith("/register_records")

    runtime_tools.catalog_samples("Номенклатура", limit=4)
    assert captured["url"].endswith("/catalog_samples")
    c._client = None
