# -*- coding: utf-8 -*-
"""Т3-N: бейджи overrides в существующих турах — поле добавляется только
при наличии перехватов (обратная совместимость); работают и без стека.

triggers_on_write/who_touches не покрываются здесь: сами тула легаси-
негерметичны в мини-фикстуре (нет event_subscriptions/metadata_code_usages
— P2), их бейдж-вставка — тот же однострочный паттерн.
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def test_read_function_badge(mini_reader):
    out = tools.read_function("ЭкспортнаяФункция")
    assert out["overrides"]["count"] == 3 and out["overrides"]["instead"] == 2
    assert out["overrides"]["hint"] == "effective_method"


def test_object_overview_badge(mini_reader):
    out = tools.object_overview("ОбъектТест")
    assert out["overrides"]["count"] >= 3


def test_no_badge_without_overrides(mini_reader):
    out = tools.read_function("ОбработкаПроведения")
    assert "overrides" not in out
    out2 = tools.object_overview("ДокЗаказ")
    assert "overrides" not in out2


def test_find_in_forms_marker_only_with_object_filter(mini_reader):
    """list-ответ find_in_forms: маркер-элемент только при фильтре по
    объекту с перехватами; без фильтра формат прежний."""
    out = tools.find_in_forms("Комментарий", object_name="ОбъектТест")
    markers = [x for x in out if "_overrides" in x]
    assert len(markers) == 1 and markers[0]["_overrides"]["count"] == 3
    out2 = tools.find_in_forms("Комментарий")
    assert not any("_overrides" in x for x in out2)
    out3 = tools.find_in_forms("Комментарий", object_name="ДокЗаказ")
    assert not any("_overrides" in x for x in out3)
