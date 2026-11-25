# -*- coding: utf-8 -*-
"""Per-chat словари токенов: изоляция, TTL, лимит+сброс (Т2 arch §2)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime import anonymizer as am  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_chats():
    """Изоляция тестов: глобальный per-chat словарь пуст до и после."""
    am._chats.clear()
    yield
    am._chats.clear()


def test_chats_isolated():
    a = am.get_anonymizer("chat-A")
    b = am.get_anonymizer("chat-B")
    t = a.anonymize_value("ООО Ромашка", "Контрагент")
    assert t == "Контрагент_Т1"
    assert b.deanonymize_value(t) is None          # чужой чат не знает токен
    assert b.anonymize_value("ООО Поставщик", "Контрагент") == "Контрагент_Т1"  # свой Т1


def test_default_chat_is_panel():
    a = am.get_anonymizer(None)
    p = am.get_anonymizer("panel")
    assert a is p
    assert a.anonymize_value("Значение X", "Значение") == p.anonymize_value("Значение X", "Значение")


def test_ttl_lazy_purge():
    a = am.get_anonymizer("ttl-chat")
    a.anonymize_value("v", "Значение")
    am._chats["ttl-chat"]["touched"] -= am._CHAT_TTL_SEC + 1
    assert "ttl-chat" not in am.all_stats()         # вычищен лениво
    a2 = am.get_anonymizer("ttl-chat")              # создался заново
    assert a2.anonymize_value("v", "Значение") == "Значение_Т1"  # счётчики с нуля


def test_overflow_reset_with_counter():
    """Сброс происходит при следующем get_anonymizer: словарь пуст,
    счётчик resets = 1."""
    a = am.get_anonymizer("limit-chat")
    a._forward.update({f"v{i}": f"Значение_Т{i}"
                       for i in range(am._CHAT_TOKEN_LIMIT + 5)})
    a2 = am.get_anonymizer("limit-chat")            # обращение запускает сброс
    assert a2 is a
    assert len(a2._forward) == 0                    # честный сброс
    assert am.chat_resets("limit-chat") == 1


def test_chat_resets_default_zero():
    assert am.chat_resets("нет-такого-чата") == 0
