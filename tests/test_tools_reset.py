# -*- coding: utf-8 -*-
"""Тест tools.reset_reader(): закрывает соединение и чистит кэш (P24)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


class _StubReader:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_reset_reader_closes_and_clears(monkeypatch):
    stub = _StubReader()
    monkeypatch.setattr(tools, "_reader", stub)
    monkeypatch.setattr(tools, "_reader_db", Path("x.db"))
    tools.reset_reader()
    assert stub.closed is True
    assert tools._reader is None
    assert tools._reader_db is None


def test_reset_reader_idempotent_on_none():
    tools.reset_reader()  # _reader уже None — не должно падать
    assert tools._reader is None


def test_reset_runtime_caches_clears_repomap(monkeypatch):
    """P1-3a: сброс рантайм-кэшей (смена конфигурации/пересборка) чистит
    и кэш repomap — рейтинг прежней базы не переживает переключение."""
    from src.facade import registry_tools, repomap, semantic_tools
    # копия _sem: update до None в сбросе не заденет реальное состояние
    monkeypatch.setattr(semantic_tools, "_sem", dict(semantic_tools._sem))
    monkeypatch.setattr(repomap, "_cache", {(str(Path("x.db")), "Obj"): []})
    registry_tools.reset_runtime_caches()
    assert repomap._cache == {}
