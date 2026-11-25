# -*- coding: utf-8 -*-
"""Парсер Predefined.xml выгрузки (предопределённые элементы) — generic.

Каркас (сверен с реальной выгрузкой): PredefinedData → Item* с Name, Code,
Description, IsFolder. Прочие дети Item: текстовый узел → скаляр в extra;
узел с детьми → таблица (имя узла = имя ТЧ, каждый ребёнок — строка-словарь
тег→текст). Это переживает неизвестные заранее детали формата (напр., ТЧ
ВидыСубконто у предопределённых счетов).
"""
from __future__ import annotations

import xml.etree.ElementTree as Et
from pathlib import Path

_KNOWN = {"name", "code", "description", "isfolder"}


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_predefined(path: str | Path) -> list[dict] | None:
    """Элементы предопределённых: [{name, code, description, is_folder,
    extra: {тег: текст}, tables: {имя_узла: [строки-словари]}}].
    None — файл не является Predefined.xml / не читается."""
    try:
        root = Et.parse(path).getroot()
    except (Et.ParseError, OSError):
        return None
    if _local(root.tag) != "PredefinedData":
        return None
    out: list[dict] = []
    for item in root.iter():
        if _local(item.tag) != "Item":
            continue
        entry: dict = {"name": "", "code": "", "description": "",
                       "is_folder": False, "extra": {}, "tables": {}}
        for child in item:
            key = _local(child.tag)
            key_lower = key.lower()
            text = (child.text or "").strip()
            if key_lower in _KNOWN:
                if key_lower == "isfolder":
                    entry["is_folder"] = text.lower() == "true"
                else:
                    entry[key_lower] = text
            elif len(child) == 0 and text:
                entry["extra"][key] = text          # скалярное поле
            else:
                rows = []
                for row in child:                    # таблица: дети-строки
                    rows.append({_local(c.tag): (c.text or "").strip()
                                 for c in row})
                if rows:
                    entry["tables"][key] = rows
        out.append(entry)
    return out
