# -*- coding: utf-8 -*-
"""Публикация F (compass-rjq): глобальный Host-гейт на всё приложение.

Аудит В3: DNS-rebinding-страница с чужим Host получала 200 от GET
owner-видов (/api/journal, /api/offline/tasks) и всего /mcp — Origin/Host
гейт security-волны 2 висел только на панельных POST. Теперь
HostGateMiddleware (src/main.py) оборачивает приложение целиком (панель,
/mcp, /health, ассеты): не-localhost Host → 403 {"error": "host not
allowed"}; отсутствующий Host (HTTP/1.0) — пропуск.
"""
import ast
import asyncio
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src import session_activity  # noqa: E402
from src.http_guard import is_local_host_name  # noqa: E402
from src.main import ChatNameMiddleware, HostGateMiddleware, mcp  # noqa: E402
from src.runtime import offline  # noqa: E402
from src.runtime import tasks as tt  # noqa: E402

# Позитивные запросы — как из браузера с localhost; чужой Host — DNS-rebinding
LOCAL_BASE = "http://localhost:8123"
EVIL_HOST = "rebind.attacker.example"

_SRC_MAIN = Path(__file__).parent.parent / "src" / "main.py"


def _build_app():
    """Стек как в main(): Host-гейт внешним слоем, затем имя чата."""
    return HostGateMiddleware(
        ChatNameMiddleware(mcp.http_app(stateless_http=True)))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Песочница офлайн-реестра (как test_security_wave2) + localhost base."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    monkeypatch.setattr(tt, "_registry", None, raising=False)
    with TestClient(_build_app(), base_url=LOCAL_BASE) as c:
        yield c
    if tt._registry is not None:
        tt._registry._stop.set()  # watcher останавливаем руками


# --------------------------------------------- юнит: middleware на raw-ASGI

class _RecordingApp:
    """Внутреннее приложение: считает вызовы, отвечает 200."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run_middleware(headers):
    """Прогон HostGateMiddleware с http-scope и заданными headers.

    Возвращаемое значение: (inner, messages) — внутреннее приложение и
    список исходящих ASGI-сообщений (ответ гейта, если был).
    """
    inner = _RecordingApp()
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "GET",
             "path": "/", "query_string": b"", "headers": headers}
    asyncio.run(HostGateMiddleware(inner)(scope, receive, send))
    return inner, messages


def test_foreign_host_denied_before_app():
    """Чужой Host → 403 JSON-отказ; внутреннее приложение не вызывается."""
    inner, messages = _run_middleware([(b"host", b"rebind.attacker.example")])
    assert inner.calls == 0
    start = messages[0]
    assert start["type"] == "http.response.start" and start["status"] == 403
    body = messages[1]
    assert body == {"type": "http.response.body",
                    "body": b'{"error": "host not allowed"}'}


def test_no_host_header_passes():
    """HTTP/1.0 без Host — пропуск (не-браузерные клиенты)."""
    inner, messages = _run_middleware([])
    assert inner.calls == 1
    assert messages[-1]["body"] == b"ok"


@pytest.mark.parametrize("host", [
    "127.0.0.1:8240", "localhost:8240", "[::1]:8240", "[::1]",
    "localhost", "127.0.0.1", "LOCALHOST:8240",
])
def test_localhost_hosts_pass(host):
    """localhost-семейство (порт/IPv6-скобки/регистр) — пропуск."""
    inner, _ = _run_middleware([(b"host", host.encode("ascii"))])
    assert inner.calls == 1


# -------------------------------------- юнит: хелпер имён (после выноса)

@pytest.mark.parametrize("value", [
    "localhost", "localhost:8240", "http://localhost:8123",
    "127.0.0.1", "127.0.0.1:8240", "http://127.0.0.1:8123",
    "[::1]", "[::1]:8240", "http://[::1]:8123", "127.77.0.254:8240",
])
def test_local_names_accepted(value):
    assert is_local_host_name(value) is True


@pytest.mark.parametrize("value", [
    "", "rebind.attacker.example", "rebind.attacker.example:8240",
    "http://evil.example", "127.0.0.1.evil.example",  # суффикс-обман
    "2130706433",          # 127.0.0.1 десятичной записью — не парсится
    "0177.0.0.1",          # восьмеричная запись — не парсится
    "192.168.1.50:8240",   # LAN-адрес — не loopback
])
def test_foreign_names_rejected(value):
    assert is_local_host_name(value) is False


# ------------------------------------------- поведенческие: живой стек

@pytest.mark.parametrize("path", [
    "/api/journal",            # owner-вид (аудит: отдавался с 200)
    "/api/offline/tasks",      # owner-вид (аудит: отдавался с 200)
    "/api/status",             # GET-сводка панели
    "/health",                 # health-эндпоинт
    "/ui",                     # страница панели
    "/mcp",                    # MCP-транспорт (GET-проба)
])
def test_get_with_foreign_host_403(client, path):
    """GET с чужим Host (DNS-rebinding) → 403 до логики роутов."""
    r = client.get(path, headers={"Host": EVIL_HOST})
    assert r.status_code == 403
    assert r.json() == {"error": "host not allowed"}


def test_post_reindex_with_foreign_host_403(client):
    """POST /reindex с чужим Host — гейт внешним слоем срабатывает первым
    (раньше это ловил только роутовый Origin/Host-гейт)."""
    r = client.post("/reindex", headers={"Host": EVIL_HOST})
    assert r.status_code == 403
    assert r.json() == {"error": "host not allowed"}


def test_assets_with_foreign_host_403(client):
    """Статика ассетов панели — за тем же гейтом."""
    r = client.get("/assets/app.js", headers={"Host": EVIL_HOST})
    assert r.status_code == 403


def test_localhost_requests_not_blocked(client, monkeypatch):
    """localhost-варианты Host проходят гейт: дальше обычная логика роутов
    (/health → 200; /api/journal за токеном → 401; /mcp → не 403)."""
    monkeypatch.setattr(config_module.config, "admin_token", "tok7")
    for host in ("127.0.0.1:8240", "localhost:8240", "[::1]:8240"):
        assert client.get("/health", headers={"Host": host}).status_code == 200
        r = client.get("/api/journal", headers={"Host": host})
        assert r.status_code == 401, host  # гейт пройден, отказ по токену
        assert client.get("/mcp", headers={"Host": host}).status_code != 403


def test_gate_runs_before_session_activity(client, monkeypatch):
    """Гейт ВНЕ ChatNameMiddleware: rebinding-проба не тикает активность
    чата (touch не вызывается для заблокированного запроса)."""
    touched = []
    monkeypatch.setattr(session_activity, "touch",
                        lambda name: touched.append(name))
    r = client.get("/api/journal", headers={"Host": EVIL_HOST})
    assert r.status_code == 403
    assert touched == []


def test_panel_post_guard_still_uses_helper(client):
    """Роутовый POST-гейт (panel_api) работает на том же хелпере после
    выноса: чужой Origin → 403 (не «host not allowed», а свой отказ)."""
    r = client.post("/api/offline/tasks/nope/cancel",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.json() == {"error": "origin forbidden"}


# ------------------------------------------------- сборка стека в main()

def test_main_wraps_app_with_host_gate():
    """main() собирает стек HostGateMiddleware(ChatNameMiddleware(...)) —
    гейт внешним слоем, до имени чата и роутов."""
    tree = ast.parse(_SRC_MAIN.read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    src_main = ast.unparse(main_fn)
    assert "HostGateMiddleware(ChatNameMiddleware(" in src_main
