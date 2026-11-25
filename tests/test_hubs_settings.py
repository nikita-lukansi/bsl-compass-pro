# -*- coding: utf-8 -*-
"""compass-85s (часть b1): входные хабы EXT_HUB/DOCS_HUB.

1) Строчный upsert .env: замена/добавление только целевых ключей,
   чужие строки и комментарии не трогаются.
2) /api/status — секция hubs (env-факты, пустые → дефолты compose).
3) /api/dumps/available — списки ext/docs-хабов.
4) POST /api/settings/hubs — гейт _authorized, валидация, запись
   с бэкапом .env.bak-<unix-ts>.
"""
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src import main as main_module  # noqa: E402
from src import state  # noqa: E402
from src.main import mcp, upsert_env_keys  # noqa: E402
from src.runtime import offline  # noqa: E402
from src.runtime import tasks as tt  # noqa: E402

APPLIED = "после рестарта контейнера (docker compose up -d --force-recreate)"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Песочница офлайн-реестра (как в test_http_smoke) + env-хабы в
    дефолтах compose: секция hubs должна показывать предсказуемое."""
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    monkeypatch.setattr(tt, "_registry", None, raising=False)
    for var in ("DUMPS_HUB", "EXT_HUB", "DOCS_HUB"):
        monkeypatch.delenv(var, raising=False)
    # base_url localhost — POST за Host-гейтом P1-1 (аудит 4l5)
    with TestClient(mcp.http_app(), base_url="http://localhost:8123") as c:
        yield c
    if tt._registry is not None:
        tt._registry._stop.set()


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """ENV_FILE маршрута направлен в tmp (маунта /env/.env на хосте нет)."""
    target = tmp_path / "env" / ".env"
    monkeypatch.setattr(main_module, "ENV_FILE", target)
    return target


# ------------------------------------------------------------- парсер .env

def test_upsert_replaces_in_place_keeps_foreign():
    text = ("# заголовок\n"
            "MCP_PORT=8240\n"
            "# DUMPS_HUB=./old  # закомментирован — не заменять\n"
            "DUMPS_HUB=./dumps\n"
            "ADMIN_TOKEN=\n")
    new_text, saved = upsert_env_keys(text, {"DUMPS_HUB": "D:/1C/Dumps"})
    assert saved == ["DUMPS_HUB"]
    lines = new_text.splitlines()
    assert lines[0] == "# заголовок"
    assert lines[1] == "MCP_PORT=8240"
    assert lines[2] == "# DUMPS_HUB=./old  # закомментирован — не заменять"
    assert lines[3] == "DUMPS_HUB=D:/1C/Dumps"
    assert lines[4] == "ADMIN_TOKEN="
    assert new_text.endswith("\n")


def test_upsert_appends_missing_keys():
    text = "MCP_PORT=8240\n"
    new_text, saved = upsert_env_keys(
        text, {"DUMPS_HUB": "./dumps", "EXT_HUB": "./ext-dumps"})
    assert saved == ["DUMPS_HUB", "EXT_HUB"]
    assert new_text == "MCP_PORT=8240\nDUMPS_HUB=./dumps\nEXT_HUB=./ext-dumps\n"


def test_upsert_into_empty_text_creates_file_body():
    new_text, saved = upsert_env_keys("", {"EXT_HUB": "D:/1C/Ext"})
    assert saved == ["EXT_HUB"]
    assert new_text == "EXT_HUB=D:/1C/Ext\n"


def test_upsert_idempotent():
    text = "# c\nEXT_HUB=./ext-dumps\n"
    once, _ = upsert_env_keys(text, {"EXT_HUB": "./ext-dumps"})
    twice, saved = upsert_env_keys(once, {"EXT_HUB": "./ext-dumps"})
    assert twice == once and saved == ["EXT_HUB"]


# ------------------------------------------------------- секция hubs статуса

def test_status_hubs_defaults(client):
    body = client.get("/api/status").json()
    assert body["hubs"] == {"dumps_hub": "./dumps",
                            "ext_hub": "./ext-dumps",
                            "docs_hub": "./docs-dumps"}


def test_status_hubs_echoes_env(client, monkeypatch):
    """compass-bcl: compose пробрасывает DUMPS_HUB/EXT_HUB/DOCS_HUB в
    environment — секция hubs показывает фактические пути из .env, а не
    дефолты."""
    monkeypatch.setenv("DUMPS_HUB", "D:/1C/Dumps")
    monkeypatch.setenv("EXT_HUB", "D:/1C/ExtDumps")
    monkeypatch.setenv("DOCS_HUB", "D:/1C/DocsDumps")
    body = client.get("/api/status").json()
    assert body["hubs"] == {"dumps_hub": "D:/1C/Dumps",
                            "ext_hub": "D:/1C/ExtDumps",
                            "docs_hub": "D:/1C/DocsDumps"}


def test_hub_dir_uses_env_when_path_exists(tmp_path, monkeypatch):
    """compass-bcl: guard хабов — env-путь берётся только если существует
    здесь (host-run); compose пробрасывает host-значения, которых в
    контейнере нет."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("DUMPS_HUB", str(hub))
    assert state._hub_dir("DUMPS_HUB", "/dumps") == hub


def test_hub_dir_falls_back_to_mount_point(monkeypatch):
    for val in ("D:/1C/Dumps", "./no-such-hub-compass", ""):
        monkeypatch.setenv("DUMPS_HUB", val)
        assert state._hub_dir("DUMPS_HUB", "/dumps") == Path("/dumps")
    monkeypatch.delenv("DUMPS_HUB", raising=False)
    assert state._hub_dir("DUMPS_HUB", "/dumps") == Path("/dumps")


def test_available_lists_ext_and_docs_hubs(client, monkeypatch, tmp_path):
    ext = tmp_path / "ext"
    docs = tmp_path / "docs"
    (ext / "Расш1").mkdir(parents=True)
    (ext / "file.cfe").write_text("x", encoding="utf-8")  # файл — не папка
    (docs / "ka25doc").mkdir(parents=True)
    monkeypatch.setattr(state, "EXT_HUB", ext)
    monkeypatch.setattr(state, "DOCS_HUB", docs)
    body = client.get("/api/dumps/available").json()
    assert [d["folder"] for d in body["ext"]] == ["Расш1"]
    assert body["ext"][0]["path"] == str(ext / "Расш1")
    assert [d["folder"] for d in body["docs"]] == ["ka25doc"]


# ------------------------------------------------------------ роут hubs

def test_hubs_route_gated_by_token(client, monkeypatch, env_file):
    monkeypatch.setattr(config_module.config, "admin_token", "test-token")
    assert client.post("/api/settings/hubs",
                       json={"ext_hub": "./ext-dumps"}).status_code == 401
    ok = client.post("/api/settings/hubs", json={"ext_hub": "./ext-dumps"},
                     headers={"X-Admin-Token": "test-token"})
    assert ok.status_code == 200


def test_hubs_route_validation(client, env_file):
    for payload, err_part in (
            ({"ext_hub": ""}, "не может быть пустым"),
            ({"ext_hub": "a\nb"}, "переносы строк"),
            ({"ext_hub": "a\r\nrm -rf"}, "переносы строк"),
            ({}, "нет полей")):
        r = client.post("/api/settings/hubs", json=payload)
        assert r.status_code == 400, payload
        assert err_part in r.json()["error"], payload


def test_hubs_route_replaces_and_appends_with_backup(client, env_file):
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "# мой конфиг\nMCP_PORT=8240\n# EXT_HUB=./old\nDUMPS_HUB=./dumps\n",
        encoding="utf-8")
    r = client.post("/api/settings/hubs",
                    json={"dumps_hub": "D:/1C/Dumps",
                          "ext_hub": "./ext-dumps"})
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] == ["DUMPS_HUB", "EXT_HUB"]
    assert body["applied"] == APPLIED
    result = env_file.read_text(encoding="utf-8")
    lines = result.splitlines()
    assert lines[0] == "# мой конфиг" and lines[1] == "MCP_PORT=8240"
    assert "# EXT_HUB=./old" in lines  # комментарий сохранился
    assert "DUMPS_HUB=D:/1C/Dumps" in lines
    assert "EXT_HUB=./ext-dumps" in lines
    assert result.index("DUMPS_HUB=D") < result.index("EXT_HUB=./ext-dumps")
    backups = list(env_file.parent.glob(".env.bak-*"))
    assert len(backups) == 1
    assert "DUMPS_HUB=./dumps" in backups[0].read_text(encoding="utf-8")


def test_hubs_route_creates_missing_env_file(client, env_file):
    env_file.parent.mkdir(parents=True)
    r = client.post("/api/settings/hubs", json={"docs_hub": "./docs-dumps"})
    assert r.status_code == 200
    assert r.json()["saved"] == ["DOCS_HUB"]
    assert env_file.read_text(encoding="utf-8") == "DOCS_HUB=./docs-dumps\n"
    assert not list(env_file.parent.glob(".env.bak-*"))  # бэкапировать нечего


def test_hubs_route_env_dir_conflict(client, env_file):
    # маунт несуществующего файла Docker создаёт каталогом — честная ошибка
    env_file.parent.mkdir(parents=True)
    env_file.mkdir()  # теперь ENV_FILE — каталог
    r = client.post("/api/settings/hubs", json={"ext_hub": "./x"})
    assert r.status_code == 409
    assert "каталог" in r.json()["error"]
