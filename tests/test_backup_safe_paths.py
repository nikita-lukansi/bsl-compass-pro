# -*- coding: utf-8 -*-
"""Аудит SAFE-4: границы хабов бэкапов (движковая защита restore)."""
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config as config_module  # noqa: E402
from src.backup import _ensure_within  # noqa: E402
from src.main import mcp  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    """Гейт открыт (ADMIN_TOKEN пуст) — за гейтом проверяем валидацию имён.
    base_url localhost — POST-роуты за Host-гейтом P1-1 (аудит 4l5)."""
    monkeypatch.setattr(config_module.config, "admin_token", "")
    with TestClient(mcp.http_app(), base_url="http://localhost:8123") as c:
        yield c


def test_ensure_within(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    inside = hub / "x"
    inside.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert _ensure_within(inside, hub, other) == inside.resolve()
    with pytest.raises(ValueError):
        _ensure_within(other, hub)


def test_backup_routes_use_validated_name(client):
    """Роуты бэкапов валидируют alias/dir (path traversal): кривое имя —
    400 с «недопустимый…» ДО какого-либо обращения к хабам бэкапов."""
    r = client.get("/api/backups", params={"alias": "../../etc"})
    assert r.status_code == 400
    assert "недопустимый alias" in r.json()["error"]
    r = client.post("/api/inbox/apply", params={"dir": "../../x"})
    assert r.status_code == 400
    assert "недопустимый dir" in r.json()["error"]
    r = client.post("/api/backup/restore", params={"dir": "../../x"})
    assert r.status_code == 400
    assert "недопустимые dir/alias" in r.json()["error"]
