# -*- coding: utf-8 -*-
"""Security-волна 2 (аудит 4l5 → compass-7lq): поведенческие тесты правок.

P1-1 — Origin/Host-гейт (403) + Content-Type application/json (415) для
мутирующих POST (панельные роуты + POST /reindex); при пустом ADMIN_TOKEN —
warn в /health.
P2-2 — journal.recent вынесен из /api/status за токен (GET /api/journal).
P2-3 — semantic-блок /api/status собирается в потоке: event loop не
блокируется даже при «зависшем» semantic_status.
P3-1 — HEAD /api/offline/health без записи и без токена; GET-проба записи
за _authorized.
"""
import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src.main import mcp  # noqa: E402
from src.runtime import offline  # noqa: E402
from src.runtime import tasks as tt  # noqa: E402

# Позитивные запросы — как из браузера с localhost (Host-гейт P1-1);
# TestClient по умолчанию шлёт Host: testserver — это «чужой» хост.
LOCAL_BASE = "http://localhost:8123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Песочница офлайн-реестра (как test_http_smoke) + localhost base_url."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    monkeypatch.setattr(tt, "_registry", None, raising=False)
    with TestClient(mcp.http_app(), base_url=LOCAL_BASE) as c:
        yield c
    if tt._registry is not None:
        tt._registry._stop.set()  # watcher останавливаем руками


# ------------------------------------------------------------- P1-1: гейт

def test_mutating_origin_forbidden(client):
    """Чужой Origin на POST — 403 до всей логики хендлера (CSRF)."""
    r = client.post("/api/offline/tasks/req_x/cancel",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_mutating_origin_local_ok(client, monkeypatch):
    """Origin localhost/127.0.0.1/[::1] проходит гейт (дальше — логика
    хендлера: 401 без токена, не 403)."""
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    for origin in ("http://localhost:8123", "http://127.0.0.1:8123",
                   "http://[::1]:8123"):
        r = client.post("/api/offline/tasks/nope/cancel",
                        headers={"Origin": origin})
        assert r.status_code == 401, origin
    ok = client.post("/api/offline/tasks/nope/cancel",
                     headers={"Origin": "http://localhost:8123",
                              "X-Admin-Token": "tok7"})
    assert ok.status_code in (200, 409)  # cancel несуществующей — по логике


def test_mutating_without_origin_ok(client, monkeypatch):
    """Отсутствующий Origin (не-браузерный клиент: curl, MCP) — не блокируется."""
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    r = client.post("/api/offline/tasks/nope/cancel")
    assert r.status_code == 401  # гейт пройден, отказ только по токену


def test_mutating_host_forbidden(client):
    """Host не-localhost (DNS-rebinding) на POST — 403."""
    r = client.post("/api/offline/tasks/x/cancel",
                    headers={"Host": "rebind.attacker.example"})
    assert r.status_code == 403


def test_mutating_text_plain_rejected(client):
    """POST с телом без application/json (cross-site text/plain) — 415."""
    r = client.post("/api/offline/responses", content="{}",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    ok = client.post("/api/offline/responses", content="{}",
                     headers={"Content-Type": "application/json"})
    assert ok.status_code == 400  # гейт пройден, отказ уже по логике body


def test_mutating_charset_suffix_ok(client):
    """Content-Type с параметром (application/json; charset=utf-8) — проходит."""
    r = client.post("/api/offline/responses", content="{}",
                    headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 400  # гейт пройден (400 — валидация task_id)


def test_reindex_guarded(client):
    """POST /reindex — за тем же гейтом: чужой Origin → 403."""
    r = client.post("/reindex",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_health_warn_without_token(client, monkeypatch):
    """/health честно предупреждает: admin_token не задан (P1-1)."""
    monkeypatch.setattr(config_module.config, "admin_token", "")
    assert client.get("/health").json().get("warn") == "admin_token not set"
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    assert "warn" not in client.get("/health").json()


# -------------------------------------------------- P2-2: journal за токеном

def test_status_journal_counters_only(client):
    """/api/status: только счётчики журнала, без recent (owner-вид)."""
    r = client.get("/api/status")
    assert r.status_code == 200
    journal = r.json()["journal"]
    assert "recent" not in journal
    assert "calls_24h" in journal  # счётчики остались


def test_journal_recent_requires_token(client, monkeypatch):
    """GET /api/journal: без токена — 401, с токеном — tail последних вызовов."""
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    assert client.get("/api/journal").status_code == 401
    ok = client.get("/api/journal", headers={"X-Admin-Token": "tok7"})
    assert ok.status_code == 200 and isinstance(ok.json()["recent"], list)


# --------------------------------------- P2-3: /api/status не блокирует loop

def test_status_semantic_offloaded_to_thread(monkeypatch):
    """semantic-блок /api/status идёт через to_thread: event loop тикает,
    пока semantic_status «висит» (в проде — подъём стека при мёртвом
    qdrant; здесь — sleep в патче)."""
    from src.facade import semantic_tools

    def slow_status():
        time.sleep(0.4)
        return {"configured": False}

    monkeypatch.setattr(semantic_tools, "semantic_status", slow_status)

    async def scenario():
        app = mcp.http_app()
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(12):
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url=LOCAL_BASE) as ac:
            r = await ac.get("/api/status")
        await task
        return r, ticks

    r, ticks = asyncio.run(scenario())
    assert r.status_code == 200
    assert r.json()["semantic"] == {"configured": False}
    # при блокирующем вызове в loop тикер набрал бы ~0-2 тика за sleep(0.4)
    assert ticks >= 6


# --------------------------------------------------- P3-1: offline/health

def test_offline_health_head_no_write(client, tmp_path):
    """HEAD /api/offline/health — 200 без токена и без записи пробы
    (файл health_probe_* не появляется)."""
    r = client.head("/api/offline/health")
    assert r.status_code == 200
    queue = tmp_path / "queue"
    assert not queue.exists() or not list(queue.glob("health_probe_*"))


def test_offline_health_get_requires_token(client, monkeypatch):
    """GET /api/offline/health (проба записи) — за _authorized."""
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    assert client.get("/api/offline/health").status_code == 401
    ok = client.get("/api/offline/health",
                    headers={"X-Admin-Token": "tok7"})
    assert ok.status_code == 200 and ok.json()["writable"] is True
