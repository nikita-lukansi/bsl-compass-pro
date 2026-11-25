# -*- coding: utf-8 -*-
"""Модульность-1 (§7 №1-2, compass-c6z): runtime_brief живёт в ядре ядра.

Юнит-матрица env-логики + AST-гейт: фасад ядра (tools/registry_tools)
не поднимает слой рантайма eager-импортом — удаление слоя S3 больше не
роняет импорт ядра. Ленивые импорты внутри функций допустимы.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent


# ------------------------------------------------------- юнит-матрица

def test_brief_online_without_url(monkeypatch):
    """online + URL не задан → configured=False и честная подсказка."""
    from src.runtime_mode import runtime_brief
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    brief = runtime_brief()
    assert brief["mode"] == "online"
    assert brief["configured"] is False
    assert "RUNTIME_URL" in brief["hint"] and "RUNTIME_MODE=offline" in brief["hint"]


def test_brief_online_with_url(monkeypatch):
    from src.runtime_mode import runtime_brief
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.setenv("RUNTIME_URL", "http://1c:8080/sandbox")
    brief = runtime_brief()
    assert brief["mode"] == "online" and brief["configured"] is True
    assert brief["hint"] == ""


def test_brief_offline(monkeypatch):
    from src.runtime_mode import runtime_brief
    monkeypatch.setenv("RUNTIME_MODE", "offline")
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    brief = runtime_brief()
    assert brief["mode"] == "offline" and brief["configured"] is True
    assert brief["hint"] == ""


def test_facade_delegation_unchanged(monkeypatch):
    """Поведение через слой рантайма сохранено байт-в-байт: /api/status
    (main.py) зовёт runtime_tools._runtime_brief() — делегация в ядро."""
    from src.facade import runtime_tools as rt
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    brief = rt._runtime_brief()
    assert brief["mode"] == "online" and brief["configured"] is False
    assert "RUNTIME_URL" in brief["hint"]


# ---------------------------------------------------------- AST-гейт

def test_no_eager_runtime_layer_import():
    """В tools.py и registry_tools.py НЕТ top-level импортов из
    .runtime_tools / runtime_tools (уровень модуля tree.body) —
    eager-импорт слоя рантайма в фасад ядра разорван (§7 №1-2)."""
    for rel in ("src/facade/tools.py", "src/facade/registry_tools.py"):
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and "runtime_tools" in (node.module or ""):
                raise AssertionError(
                    f"{rel}: eager импорт слоя рантайма (аудит §7 №1-2)")
