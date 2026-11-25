# -*- coding: utf-8 -*-
"""Тесты активности сессий (session_activity) и сохранности полей
при bind_context."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import session_activity as sa  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Рабочая папка в tmp; состояние активности — чистое."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    sa._activity.clear()
    sa._persisted_at.clear()
    return settings_dir


def _read_sessions(settings_dir: Path) -> dict:
    return json.loads((settings_dir / "sessions.json").read_text(encoding="utf-8"))


def test_touch_persists_then_throttles(workspace_dir):
    """touch: первая активность пишется в файл, частые — нет, после паузы — да."""
    sa.touch("ut-chat", now=1000.0)
    data = _read_sessions(workspace_dir)
    rec = data["sessions"]["ut-chat"]
    assert rec["last_seen"].startswith("1970-01-01T00:16:40")  # epoch 1000 UTC
    assert rec["requests"] == 1

    # в пределах PERSIST_INTERVAL — записи в файл нет (счётчик только в памяти)
    sa.touch("ut-chat", now=1030.0)
    sa.touch("ut-chat", now=1050.0)
    assert _read_sessions(workspace_dir)["sessions"]["ut-chat"]["requests"] == 1
    assert sa.snapshot()["ut-chat"]["requests"] == 3

    # после паузы — свежий снапшот попадает в файл одним числом
    sa.touch("ut-chat", now=1100.0)
    rec = _read_sessions(workspace_dir)["sessions"]["ut-chat"]
    assert rec["requests"] == 4
    assert rec["last_seen"].startswith("1970-01-01T00:18:20")  # epoch 1100 UTC


def test_touch_empty_chat_defaults_to_panel(workspace_dir):
    """Пустой заголовок → чат panel (как в middleware)."""
    sa.touch("", now=1000.0)
    assert "panel" in sa.snapshot()
    assert "panel" in _read_sessions(workspace_dir)["sessions"]


def test_restore_from_file(workspace_dir):
    """restore: last_seen/requests из sessions.json поднимаются в память,
    записи без last_seen игнорируются."""
    ws.save_sessions({"sessions": {
        "old-chat": {"config_id": "Compass_UT_x", "last_seen":
                     "2026-11-05T12:00:00+00:00", "requests": 7},
        "no-activity": {"config_id": "Compass_KA_y"},
    }})
    sa.restore()
    snap = sa.snapshot()
    assert snap["old-chat"]["requests"] == 7
    assert snap["old-chat"]["last_seen"] > 0
    assert "no-activity" not in snap


def test_panel_list_merges_registry_and_activity(workspace_dir):
    """sessions_panel_list: display_name из реестра, сортировка по свежести."""
    ws.save_registry({"configs": {
        "Compass_UT_a": {"short_name": "UT", "display_name": "УТ 11.5"},
        "Compass_KA_b": {"short_name": "KA", "display_name": "КА 2.5"},
    }})
    ws.save_sessions({"sessions": {
        "ka-chat": {"config_id": "Compass_KA_b"},
    }})
    sa.touch("ut-chat", now=2000.0)   # свежее
    sa.touch("ka-chat", now=1000.0)   # старее, есть привязка

    rows = sa.sessions_panel_list()
    assert rows[0]["chat"] == "ut-chat"  # самый свежий — первый
    by_chat = {r["chat"]: r for r in rows}
    assert by_chat["ut-chat"]["display_name"] == ""  # привязки нет
    assert by_chat["ut-chat"]["requests"] == 1
    assert by_chat["ka-chat"]["display_name"] == "КА 2.5"
    assert by_chat["ka-chat"]["config_id"] == "Compass_KA_b"


def test_panel_list_limit(workspace_dir):
    """Топ limit по свежести, хвост отсекается."""
    for i in range(20):
        sa.touch(f"chat-{i:02d}", now=1000.0 + i)
    rows = sa.sessions_panel_list(limit=12)
    assert len(rows) == 12
    assert rows[0]["chat"] == "chat-19"  # самый свежий


def test_bind_context_preserves_activity_fields(workspace_dir):
    """bind_context обновляет запись точечно: last_seen/requests живут."""
    ws.save_sessions({"sessions": {
        "ut-chat": {"config_id": "Compass_UT_old",
                    "last_seen": "2026-11-05T12:00:00+00:00", "requests": 5},
    }})
    from src.context import bind_context
    result = bind_context("Compass_UT_new", chat="ut-chat")
    assert result["previous"] == "Compass_UT_old"

    rec = _read_sessions(workspace_dir)["sessions"]["ut-chat"]
    assert rec["config_id"] == "Compass_UT_new"
    assert rec["last_seen"] == "2026-11-05T12:00:00+00:00"  # не затёрт
    assert rec["requests"] == 5
