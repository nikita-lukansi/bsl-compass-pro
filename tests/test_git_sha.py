# -*- coding: utf-8 -*-
"""Тесты git SHA образа (1mj.7): env COMPASS_GIT_SHA, дефолт "dev".

Строку в ответах /health и /api/status проверяет AST-смокинг
(test_main_smoke_names) — src.main в тестовое окружение не импортируется.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import git_sha  # noqa: E402


def test_git_sha_default_dev(monkeypatch):
    """Без bake (env не задан/пуст) — честное «dev», не выдуманный SHA."""
    monkeypatch.delenv("COMPASS_GIT_SHA", raising=False)
    assert git_sha() == "dev"
    monkeypatch.setenv("COMPASS_GIT_SHA", "")
    assert git_sha() == "dev"


def test_git_sha_from_env(monkeypatch):
    """Bake при docker build: ARG GIT_SHA → ENV COMPASS_GIT_SHA → ответ."""
    monkeypatch.setenv("COMPASS_GIT_SHA", "abc1234")
    assert git_sha() == "abc1234"
