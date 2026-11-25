# -*- coding: utf-8 -*-
"""Аудит SAFE-2/6: гейт owner-GET + timing-safe токен.

Гейт — поведенчески (401 без токена, проход с токеном): гейт срабатывает
РАНЬШЕ валидаций task_id и имени req-файла, поэтому невалидные значения в
запросах не маскируют пропуск гейта. Timing-safe сравнение токена —
текстовым аудитом файла панельных роутов (panel_api.py после выноса
compass-2ao, до — main.py; локатор tests/panel_source.py)."""
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src.main import mcp  # noqa: E402
from src.runtime import offline  # noqa: E402
from src.runtime import tasks as tt  # noqa: E402
from tests.panel_source import panel_routes_source  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Песочница офлайн-реестра (как test_http_smoke): owner-роуты создают
    синглтон get_registry() с каталогами и watcher'ом — направляем в tmp_path."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    monkeypatch.setattr(tt, "_registry", None, raising=False)
    with TestClient(mcp.http_app()) as c:
        yield c
    if tt._registry is not None:
        tt._registry._stop.set()  # watcher останавливаем руками


def test_offline_owner_routes_gated(client, monkeypatch):
    """Owner-GET роуты за гейтом: без токена — 401 по всем трём, с токеном
    гейт пройден (200 списка, 404 несуществующих id/файла — по логике)."""
    monkeypatch.setattr(config_module.config, "admin_token", "audit-token")
    assert client.get("/api/offline/tasks").status_code == 401
    # гейт раньше разбора task_id — несуществующий id тоже 401
    assert client.get("/api/offline/tasks/Compass_x_0000").status_code == 401
    # гейт раньше проверки имени req-файла
    assert client.get("/api/offline/requests/req_zzz.json").status_code == 401

    headers = {"X-Admin-Token": "audit-token"}
    ok = client.get("/api/offline/tasks", headers=headers)
    assert ok.status_code == 200 and "tasks" in ok.json()
    assert client.get("/api/offline/tasks/Compass_x_0000",
                      headers=headers).status_code == 404
    assert client.get("/api/offline/requests/req_zzz.json",
                      headers=headers).status_code == 404


def test_token_compare_is_timing_safe():
    text = panel_routes_source().read_text(encoding="utf-8")
    start = text.index("def _authorized(")
    end = text.index("\ndef ", start + 1)  # конец функции
    body = text[start:end]
    assert "hmac.compare_digest" in body
    assert "import hmac" in text
    # прямое сравнение токена == удалено из _authorized
    assert "== token" not in body
