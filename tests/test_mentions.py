# -*- coding: utf-8 -*-
"""compass-5he: мост «дока ↔ код» — матчинг mentions.

Старый матчинг был регистрозависимым и не умел «фраз»: русская документация
пишет CamelCase-имя словами через пробел («Расчет себестоимости товаров» ↔
РасчетСебестоимостиТоваров) — mentions оставались пустыми.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.docs.mentions import find_mentions  # noqa: E402

NAMES = {"РасчетСебестоимостиТоваров", "ЗаказКлиента", "Номенклатура",
         "ОбъектТест"}


def test_case_insensitive_cyrillic():
    """Регистр текста не влияет (в т.ч. кириллица — python casefold)."""
    out = find_mentions("НОМЕНКЛАТУРА принята на склад.",
                        {"Номенклатура"})
    assert out == ["Номенклатура"]


def test_spaced_camelcase_phrase():
    """Кейс КА (5he): в тексте фраза словами, в индексе — CamelCase."""
    text = ("Расчет себестоимости товаров выполняется регламентным "
            "заданием еженощно.")
    out = find_mentions(text, NAMES)
    assert out == ["РасчетСебестоимостиТоваров"]


def test_spaced_phrase_requires_full_window():
    """Часть фразы («расчет себестоимости» без товаров) — не совпадение:
    окно должно равняться полной фразе имени."""
    out = find_mentions("расчет себестоимости описан в разделе.",
                        {"РасчетСебестоимостиТоваров"})
    assert out == []


def test_spaced_phrase_word_order_matters():
    out = find_mentions("товаров расчет себестоимости выполнен.",
                        {"РасчетСебестоимостиТоваров"})
    assert out == []


def test_verbatim_token_still_matches():
    out = find_mentions("В документе РасчетСебестоимостиТоваров строки "
                        "заполняются автоматически.", NAMES)
    assert "РасчетСебестоимостиТоваров" in out


def test_ranking_by_occurrences_and_limit():
    text = ("номенклатура номенклатура номенклатура; заказ клиента; "
            "заказ клиента")
    out = find_mentions(text, NAMES, limit=1)
    assert out == ["Номенклатура"]
    out2 = find_mentions(text, NAMES)
    assert out2[0] == "Номенклатура" and len(out2) == 2


def test_stop_words_and_short_tokens_ignored():
    out = find_mentions("форма окно поле для и the", NAMES)
    assert out == []


def test_empty_inputs():
    assert find_mentions("", NAMES) == []
    assert find_mentions("текст", set()) == []


def test_punctuation_does_not_break_phrase():
    out = find_mentions("Расчет себестоимости товаров (регл.): итоги.",
                        {"РасчетСебестоимостиТоваров"})
    assert out == ["РасчетСебестоимостиТоваров"]


def test_underscore_names_not_phrase_matched():
    """Имена с «_» и составные с «.» матчатся токеном, не фразой."""
    out = find_mentions("номенклатура_т1 используется в документе.",
                        {"Номенклатура_Т1"})
    assert out == ["Номенклатура_Т1"]
