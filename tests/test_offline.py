# -*- coding: utf-8 -*-
"""Тесты TokenAnonymizer.

Файловый обмен переехал в tasks.py (Т2 фаза 1); здесь — только анонимизатор.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime.anonymizer import TokenAnonymizer  # noqa: E402


def test_anonymizer_roundtrip():
    anon = TokenAnonymizer()
    token1 = anon.anonymize_value("ООО Ромашка", "Контрагент")
    token2 = anon.anonymize_value("ООО Ромашка", "Контрагент")  # тот же → тот же токен
    token3 = anon.anonymize_value("ИП Иванов", "Контрагент")
    assert token1 == token2
    assert token1 != token3
    assert token1.startswith("Контрагент_Т")

    assert anon.deanonymize_value(token1) == "ООО Ромашка"
    assert anon.deanonymize_value(token3) == "ИП Иванов"
    assert anon.deanonymize_value("нет_токена") is None


def test_anonymize_data_nested():
    anon = TokenAnonymizer()
    data = {
        "НаименованиеКонтрагента": "ООО Ромашка",
        "Код": "0001",  # не маскируется (нет паттерна)
        "Товары": [
            {"Наименование": "Хлеб белый", "Количество": 5},
            {"Наименование": "Молоко", "Количество": 3},
        ],
    }
    result = anon.anonymize_data(data)
    assert result["НаименованиеКонтрагента"].startswith("Контрагент_Т")
    assert result["Код"] == "0001"  # числа/коды не тронуты
    assert result["Товары"][0]["Количество"] == 5


def test_deanonymize_text():
    anon = TokenAnonymizer()
    anon.anonymize_value("ООО Ромашка", "Контрагент")
    code = "Контрагент = Контрагент_Т1; Контрагент.Наименование = «Ромашка»;"
    restored = anon.deanonymize_text(code)
    assert "ООО Ромашка" in restored
    assert "Контрагент_Т1" not in restored


def test_anonymizer_stats_and_clear():
    anon = TokenAnonymizer()
    anon.anonymize_value("А", "Значение")
    anon.anonymize_value("Б", "Значение")
    st = anon.stats()
    assert st["tokens"] == 2
    anon.clear()
    assert anon.stats()["tokens"] == 0


# ------------------------------------------------- compass-lz7: сквозная маскировка ответа

def test_anonymize_response_masks_every_string():
    """Все строковые значения → токены (Код тоже — канал утечки №3);
    числа/булевы/None — не строки, проходят; ключи не маскируются;
    одинаковые значения → один токен."""
    anon = TokenAnonymizer()
    data = {
        "Наименование": "Меотида ООО",
        "Код": "00-00000001",
        "НомераГТД": [{"Код": "10013020/260916/0001234"}],
        "Товары": [
            {"Номенклатура.Наименование": "Chapman Рэд КССЛ", "Количество": 5},
            {"Номенклатура.Наименование": "Chapman Рэд КССЛ", "Количество": True},
        ],
        "Пустое": "",
        "Ничего": None,
    }
    out = anon.anonymize_response(data)
    assert out["Наименование"].startswith("Значение_Т")
    assert out["Код"].startswith("Значение_Т")
    assert out["НомераГТД"][0]["Код"].startswith("Значение_Т")
    assert out["Товары"][0]["Номенклатура.Наименование"].startswith(
        "Номенклатура_Т")
    assert out["Товары"][0]["Количество"] == 5
    assert out["Товары"][1]["Количество"] is True
    assert out["Пустое"] == "" and out["Ничего"] is None
    assert (out["Товары"][0]["Номенклатура.Наименование"]
            == out["Товары"][1]["Номенклатура.Наименование"])


def test_anonymize_response_whitelist_dates_and_guids():
    """Whitelist (compass-lz7): ISO-даты (с вариантами времени/таймзоны)
    и GUID проходят; «почти дата»/«обрезанный GUID» маскируются."""
    anon = TokenAnonymizer()
    passed = ["2026-09-16",
              "2026-09-16T12:30:00",
              "2026-09-16 12:30:00",
              "2026-09-16T12:30:00.123456",
              "2026-09-16T12:30:00+03:00",
              "e1c4a2b3-0000-1111-2222-333344445555",
              "E1C4A2B3-0000-1111-2222-333344445555"]
    data = {str(i): v for i, v in enumerate(passed)}
    out = anon.anonymize_response(data)
    for i, v in enumerate(passed):
        assert out[str(i)] == v
    masked = ["Приобретение от 16.09.2026",       # дата не ISO на старте
              "2026-09-16 и хвост",               # дата с прицепом
              "e1c4a2b3-0000-1111-2222-33334444555"]  # 35 символов
    for v in masked:
        assert anon.anonymize_response({"x": v})["x"].startswith("Значение_Т")


def test_anonymize_response_meaningful_prefixes_and_roundtrip():
    """Префиксы из списка «сущность+поле» (Контрагент_Т3), прочее —
    Значение_ТN; deanonymize_data обратим для новых токенов."""
    anon = TokenAnonymizer()
    data = {"Контрагент": "ООО Ромашка",
            "Организация.Наименование": "Меотида ООО",
            "Комментарий": "заметка",
            "Ссылка": "Приобретение ЛД00-000998"}
    out = anon.anonymize_response(data)
    assert out["Контрагент"].startswith("Контрагент_Т")
    assert out["Организация.Наименование"].startswith("Организация_Т")
    assert out["Комментарий"].startswith("Комментарий_Т")
    assert out["Ссылка"].startswith("Значение_Т")
    restored = anon.deanonymize_data(out)
    assert restored == data
