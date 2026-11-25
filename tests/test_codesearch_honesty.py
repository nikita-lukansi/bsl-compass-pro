# -*- coding: utf-8 -*-
"""Честность codesearch (compass-50a): сбой Qdrant НЕ «индекс пуст»
(контракт count() — упавший сервер отличим от пустой коллекции);
расхождение реестр↔Qdrant не молчит."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import semantic_tools as st  # noqa: E402
from src.facade import tools as facade_tools  # noqa: E402


class _Col:
    """Фейковая коллекция: счёт или исключение."""

    def __init__(self, count=0, exc=None):
        self._count = count
        self._exc = exc

    def count(self):
        if self._exc:
            raise self._exc
        return self._count


@pytest.fixture
def search_env(tmp_path, monkeypatch):
    """reader есть, стек собран, коллекция подменяется тестом.

    compass-sm4: без активной конфигурации — режимную ветку codesearch
    не задеваем (герметичность к settings машины, где дефолт-привязка
    может стоять в fast)."""
    monkeypatch.setattr(facade_tools, "reader", lambda: object())
    monkeypatch.setattr(st, "_ensure_stack", lambda: True)
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    yield st._sem


def test_codesearch_qdrant_down_is_error_not_empty(search_env, monkeypatch):
    """Сервер недоступен → честный error, не «индекс пуст»."""
    monkeypatch.setitem(st._sem, "collection",
                        _Col(exc=ConnectionError("refused")))
    out = st.codesearch("скидки")
    assert out.get("error") and "Qdrant не ответил" in out["error"]
    assert "info" not in out


def test_codesearch_empty_is_info(search_env, monkeypatch):
    monkeypatch.setitem(st._sem, "collection", _Col(count=0))
    out = st.codesearch("скидки")
    assert "векторный индекс пуст" in out.get("info", "")


def test_codesearch_empty_with_registry_points_flags_discrepancy(
        search_env, monkeypatch):
    """Реестр говорит 653755, Qdrant — 0: пометка расхождения в info."""
    monkeypatch.setitem(st._sem, "collection", _Col(count=0))
    monkeypatch.setattr("src.context.active_config_id",
                        lambda: "Compass_KA_798247c2")
    import src.registry as registry_mod

    def fake_get_config(cid):
        return {"mode": "full", "vectors": {"points": {"code": 653755}}}

    monkeypatch.setattr(registry_mod, "get_config", fake_get_config)
    out = st.codesearch("скидки")
    assert "векторный индекс пуст" in out.get("info", "")
    assert "653755" in out.get("discrepancy", "")


def test_codesearch_no_discrepancy_when_registry_empty(search_env,
                                                       monkeypatch):
    """Реестр тоже пуст (vectors=0, УТ) — info без пометки."""
    monkeypatch.setitem(st._sem, "collection", _Col(count=0))
    monkeypatch.setattr("src.context.active_config_id",
                        lambda: "Compass_UT11_5_22_97e577c6")
    import src.registry as registry_mod

    monkeypatch.setattr(registry_mod, "get_config",
                        lambda cid: {"mode": "full"})
    out = st.codesearch("скидки")
    assert "discrepancy" not in out
