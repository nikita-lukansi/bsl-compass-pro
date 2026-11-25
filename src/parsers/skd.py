# -*- coding: utf-8 -*-
"""Парсер схем СКД (DataComposition) на лету — clean-room, stdlib.

Вызывается тулом get_skd_info по пути к Template XML; ничего не индексирует:
файл читается при каждом вызове (запросы СКД меняются редко, а чтение
одного файла — миллисекунды).
"""
from __future__ import annotations

import xml.etree.ElementTree as Et
from pathlib import Path


def parse_skd(path: str | Path) -> dict | None:
    """Разбор схемы компоновки: наборы данных с текстами запросов, параметры.

    Возвращает {data_sets: [{name, query}], parameters: [имена],
    calculated_fields: [имена]} или None, если файл — не СКД.
    """
    try:
        root = Et.parse(path).getroot()
    except (Et.ParseError, OSError):
        return None
    if "DataCompositionSchema" not in root.tag:
        return None

    def _local(tag: str) -> str:
        return tag.split("}")[-1]

    def _ns_all(tag: str) -> str:
        # поиск ребёнка по локальному имени при любом namespace
        return f".//{{}}{tag}"

    data_sets: list[dict] = []
    for ds in root.iter():
        if _local(ds.tag) != "dataSet":
            continue
        name = ""
        query = ""
        fields: list[str] = []
        for child in ds:
            t = _local(child.tag)
            if t == "name":
                name = (child.text or "").strip()
            elif t == "source":
                for sub in child.iter():
                    if _local(sub.tag) == "query":
                        query = (sub.text or "").strip()
            elif t == "field":
                # набор-объект: перечисление полей (dataPath)
                dp = child.find(_ns_all("dataPath"))
                if dp is not None and dp.text:
                    fields.append(dp.text.strip())
        data_sets.append({
            "name": name,
            "kind": "query" if query else "object",
            "query": query,
            "fields": fields,
        })

    parameters = [
        (p.findtext("name") or "").strip()
        for p in root.iter()
        if _local(p.tag) == "parameter" and p.findtext("name")
    ]
    calculated = [
        (f.findtext("name") or "").strip()
        for f in root.iter()
        if _local(f.tag) == "calculatedField" and f.findtext("name")
    ]
    return {
        "data_sets": data_sets,
        "parameters": parameters,
        "calculated_fields": calculated,
    }
