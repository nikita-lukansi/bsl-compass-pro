# -*- coding: utf-8 -*-
"""Поведенческий HTTP-смок (аудит TEST-1): реальные вызовы роутов вместо
строчных ассертов. Окружение: fastmcp/uvicorn/httpx в requirements-dev —
правило «src.main не импортируется» на НОВЫЕ тесты больше не действует
(докстринги старых тестов не трогаем).
"""
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src.main import mcp  # noqa: E402
from src.runtime import offline  # noqa: E402
from src.runtime import tasks as tt  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Песочница офлайн-реестра: роут создаёт синглтон get_registry()
    с каталогами и daemon-watcher'ом — направляем в tmp_path
    (offline.py:18: COMPASS_OFFLINE_PATH/атрибут — override для тестов)."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    monkeypatch.setattr(tt, "_registry", None, raising=False)
    with TestClient(mcp.http_app()) as c:
        yield c
    if tt._registry is not None:
        tt._registry._stop.set()  # watcher останавливаем руками (как test_tasks)


def test_health_git_sha(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy" and body["git_sha"]


def test_assets_traversal_blocked(client):
    for bad in ("/assets/../env/.env", "/assets/..%2F..%2Fenv%2F.env",
                "/assets/sub/../../etc/passwd"):
        assert client.get(bad).status_code == 404, bad


def test_owner_gate_with_token(client, monkeypatch):
    # admin_token — атрибут ИНСТАНСА config (src/config.py: config = Config()),
    # патчим инстанс — патч модуля _authorized не увидит
    monkeypatch.setattr(config_module.config, "admin_token", "test-token")
    assert client.get("/api/offline/tasks").status_code == 401
    ok = client.get("/api/offline/tasks",
                    headers={"X-Admin-Token": "test-token"})
    assert ok.status_code == 200 and "tasks" in ok.json()
