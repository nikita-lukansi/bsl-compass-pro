# -*- coding: utf-8 -*-
"""Мост «дока ↔ код»: какие объекты метаданных упомянуты в странице справки.

Детерминированный слой обогащения (спецификация §5): пересечение токенов
текста с именами объектов индекса. Без LLM и без перезаписи корпуса —
поле mentions вычисляется на лету при чтении/поиске.

compass-5he: матчинг регистронезависимый (python casefold — NOCASE
SQLite кириллицу не сворачивает), и имя в тексте ищется ещё и «фразой»:
в русской документации CamelCase-имя пишется словами через пробел
(«Расчет себестоимости товаров» ↔ РасчетСебестоимостиТоваров).
"""
from __future__ import annotations

import re

# Токены: идентификаторы 1С (ЗаказКлиента, Номенклатура_Т1) и слова текста
_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{3,}")

# Граница слова внутри CamelCase: строчная/цифра → заглавная, или подряд
# заглавных перед последней заглавной + строчными (СуммаНУ → Сумма, НУ)
_CAMEL_SPLIT = re.compile(
    r"(?<=[а-яёa-z0-9])(?=[А-ЯЁA-Z])|(?<=[А-ЯЁA-Z])(?=[А-ЯЁA-Z][а-яёa-z])")

# Части речи и служебные слова, которые встречаются в любом тексте и
# затирают mentions шумом (объекты конфигурации так не называются)
_STOP = {"для", "как", "или", "все", "при", "это", "если", "ссылка", "форма",
         "поле", "поли", "кнопка", "окно", "глава", "страница", "раздел",
         "пример", "вариант", "настройка", "документы", "справочники",
         "the", "and", "for", "with"}

# Потолок длины фразы в словах (окно n-грамм текста) — имена 1С длиннее
# восьми слов не встречаются, а скан окон дорожает
_MAX_PHRASE_WORDS = 8


def normalize(names: list[str]) -> set[str]:
    """Множество имён для матчинга: точные имена + последние сегменты
    составных имён (Справочник.Номенклатура → Номенклатура).
    """
    out: set[str] = set()
    for name in names or []:
        name = name.strip()
        if not name:
            continue
        out.add(name)
        if "." in name:
            out.add(name.rsplit(".", 1)[-1])
    return out


def _spaced_phrase(name: str) -> tuple[str, ...] | None:
    """CamelCase-имя → кортеж слов в нижнем регистре для фразового
    матчинга («РасчетСебестоимостиТоваров» → («расчет», «себестоимости»,
    «товаров»)). None — имя не многословный CamelCase (нет границы
    регистра, есть «_»/«.» — матчится токеном как раньше)."""
    if "_" in name or "." in name:
        return None
    parts = _CAMEL_SPLIT.split(name)
    if len(parts) < 2 or any(not p for p in parts):
        return None
    return tuple(p.casefold() for p in parts)


def find_mentions(text: str, index_names: set[str],
                  limit: int = 20) -> list[str]:
    """Список имён объектов, встретившихся в тексте (порядок по числу вхождений).

    index_names — результат mentions.normalize() по reader.object_names().
    Матчинг регистронезависимый (casefold, кириллица в том числе).
    Токены короче 4 знаков и стоп-слова не матчатся: слишком шумно.
    """
    if not text or not index_names:
        return []
    folded: dict[str, str] = {}
    phrases: dict[tuple[str, ...], str] = {}
    for name in index_names:
        folded.setdefault(name.casefold(), name)
        phrase = _spaced_phrase(name)
        if phrase and len(phrase) <= _MAX_PHRASE_WORDS and \
                all(len(word) >= 3 for word in phrase):
            phrases.setdefault(phrase, name)

    counts: dict[str, int] = {}
    low_tokens = [t.casefold() for t in _TOKEN.findall(text)]
    for low in low_tokens:
        if len(low) < 4 or low in _STOP:
            continue
        hit = folded.get(low)
        if hit:
            counts[hit] = counts.get(hit, 0) + 1
    # фразы: подряд идущие слова текста = слова CamelCase-имени
    if phrases:
        for i in range(len(low_tokens)):
            for n in range(2, _MAX_PHRASE_WORDS + 1):
                if i + n > len(low_tokens):
                    break
                hit = phrases.get(tuple(low_tokens[i:i + n]))
                if hit:
                    counts[hit] = counts.get(hit, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:limit]]
