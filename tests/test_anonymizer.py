# -*- coding: utf-8 -*-
"""Тесты TokenAnonymizer: отсутствие дедлока + корректность словарей."""
import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime.anonymizer import TokenAnonymizer  # noqa: E402


def test_anonymize_value_no_deadlock():
    """Нового значения в отдельном потоке: раньше вложенный захват лока
    вешал поток навсегда (P1). join с таймаутом — поток обязан завершиться."""
    a = TokenAnonymizer()
    result = {}

    def worker():
        result["token"] = a.anonymize_value("ООО Ромашка", "Контрагент")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "дедлок: anonymize_value не завершился за 5 с"
    assert result["token"] == "Контрагент_Т1"


def test_roundtrip_and_counters():
    """Токен стабилен для повтора, деанонимизация возвращает исходник,
    счётчики независимы по префиксам."""
    a = TokenAnonymizer()
    t1 = a.anonymize_value("ООО Ромашка", "Контрагент")
    t1b = a.anonymize_value("ООО Ромашка", "Контрагент")
    t2 = a.anonymize_value("Иванов И.И.", "Клиент")
    assert t1 == t1b == "Контрагент_Т1"
    assert t2 == "Клиент_Т1"          # счётчики независимы по префиксам
    assert a.deanonymize_value(t1) == "ООО Ромашка"
    assert a.deanonymize_value("НетТакогоТокена") is None
