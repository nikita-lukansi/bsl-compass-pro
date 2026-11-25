# -*- coding: utf-8 -*-
"""Общие фикстуры герметичных тестов фасады (мини-индекс)."""
import pytest


@pytest.fixture
def mini_reader(tmp_path, monkeypatch):
    """Тулы фасады читают мини-индекс; reader-кэш чистится до и после."""
    from src.facade import tools
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()
