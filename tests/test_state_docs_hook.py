# -*- coding: utf-8 -*-
"""Модульность-1 (§7 №5, compass-c6z): публичный хук docs_busy в state.

Реестр больше не читает приватное _docs слоя справки — гейт «синхронизация
документации пишет в общий index.db» работает через state.DOCS_BUSY.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent


@pytest.fixture
def docs_hook():
    """Чистое состояние хука на тест (глобальный Event — чищу в finally)."""
    from src import state
    state.DOCS_BUSY.clear()
    yield state
    state.DOCS_BUSY.clear()


def test_docs_busy_flag(docs_hook):
    """set → docs_busy() True → clear → False (публичный API хука)."""
    assert docs_hook.docs_busy() is False
    docs_hook.DOCS_BUSY.set()
    assert docs_hook.docs_busy() is True
    docs_hook.DOCS_BUSY.clear()
    assert docs_hook.docs_busy() is False


def test_registry_does_not_read_docs_layer():
    """Строковый гейт §7 №5: registry.py не содержит идентификатора
    приватного _docs (не путать с легитимным config_docs_root — сверка
    по границе слова) и никаких ссылок на слой help_tools."""
    text = (_ROOT / "src" / "registry.py").read_text(encoding="utf-8")
    assert not re.search(r"\b_docs\b", text)
    assert "help_tools" not in text
