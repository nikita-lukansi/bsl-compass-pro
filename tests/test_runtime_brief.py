# -*- coding: utf-8 -*-
"""TASK-25: _runtime_brief — лёгкие env-поля рантайма (без ping базы)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_brief_offline(monkeypatch):
    from src.facade import runtime_tools as rt
    monkeypatch.setenv("RUNTIME_MODE", "offline")
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    b = rt._runtime_brief()
    assert b["mode"] == "offline" and b["configured"] is True and b["hint"] == ""


def test_brief_online_not_configured(monkeypatch):
    from src.facade import runtime_tools as rt
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    b = rt._runtime_brief()
    assert b["configured"] is False and "RUNTIME" in b["hint"]


def test_brief_online_configured(monkeypatch):
    from src.facade import runtime_tools as rt
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.setenv("RUNTIME_URL", "http://1c:8080/sandbox")
    assert rt._runtime_brief()["configured"] is True
