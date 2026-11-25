# -*- coding: utf-8 -*-
"""Досье конфигурации (Т4, agreements): детерминированный каркас из индекса
+ файлы-оверлеи владельца + тулы get_docs / search_docs / build_docs.

Двухслойная модель: каркас генерируется из index.db (подсистемы → объекты →
назначение → справка help_pages → точки входа по графу вызовов), нарратив
LLM поверх — отдельный контур (нужен провайдер, в эту задачу не входит).
Хранение: workspace/Settings/docs/<cid>/dossier.md; заметки владельца —
рядом файлами notes-*.md, регенерация их НЕ затирает. Язык — русский.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..recon.gyr_reader import GyrReader

logger = logging.getLogger(__name__)

DOSSIER_FILE = "dossier.md"
OVERLAY_PREFIX = "notes-"

def _docs_dir(cid: str) -> Path:
    """Каталог досье конфигурации: workspace/Settings/docs/<cid>/."""
    from .. import workspace
    # S3: оверлеи в системной зоне, путь динамический (env-override тестов)
    return workspace.settings_dir().parent / "docs" / cid


def _entry(cid: str) -> dict:
    from .. import registry as registry_mod
    from ..context import active_config_id
    target = (cid or "").strip() or active_config_id()
    if not target:
        raise ValueError("активная конфигурация не выбрана")
    entry = registry_mod.get_config(target)
    if entry is None:
        raise ValueError(f"конфигурация '{target}' не найдена")
    entry = dict(entry)
    entry["id"] = target
    return entry


def _object_catalog(reader: GyrReader) -> list[dict[str, Any]]:
    """Каталог объектов каркаса: синонимы + подсистемы (источник состава —
    object_synonyms; подсистемы из subsystem_content)."""
    syn = reader._rows(
        "SELECT object_name, category, synonym FROM object_synonyms "
        "ORDER BY category, object_name")
    subs = reader._rows(
        "SELECT subsystem_name, subsystem_synonym, object_ref "
        "FROM subsystem_content ORDER BY subsystem_name")
    by_name: dict[str, dict[str, Any]] = {}
    for row in syn:
        by_name[row["object_name"].casefold()] = {
            "object_name": row["object_name"], "category": row["category"],
            "synonym": row["synonym"] or "", "subsystems": [],
        }
    for row in subs:
        ref = row["object_ref"] or ""
        obj_name = ref.split(".")[-1].strip()
        rec = by_name.get(obj_name.casefold())
        if rec is None:
            # объект подсистемы без синонима — честно попадает в каркас
            rec = {"object_name": obj_name, "category": ref.split(".")[0],
                   "synonym": "", "subsystems": []}
            by_name[obj_name.casefold()] = rec
        rec["subsystems"].append(row["subsystem_name"])
    return list(by_name.values())


# Потолок методов на объект в каркасе (список; итог — честный COUNT)
DETAIL_METHODS_CAP = 200
# Размер IN-чанков — ниже SQLITE_MAX_VARIABLE_NUMBER (999 у старых SQLite)
_IN_CHUNK = 400


def _chunks(seq: list, size: int = _IN_CHUNK):
    """Имя объекта по чанкам для параметров IN (...)."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fill_details(reader: GyrReader, objects: list[dict[str, Any]]) -> None:
    """Наполнение объектов каркаса БАТЧ-проходами (compass-9cw).

    Точечный путь (N объектов x 5 запросов) на КА не укладывался в таймаут:
    у help_pages НЕТ индекса по object_name (help.rs:26 — только source_id),
    каждый объект сканировал все 8730 страниц. Здесь: справка — один проход
    по таблице + группировка в памяти; реквизиты и методы — индексные
    IN-чанки (idx_oa_object, idx_mod_object + idx_meth_module); «касаются
    извне» — по-прежнему точечные индексные запросы (callee_name
    индексирован, а общий IN на 653k имён не влезает в параметры).
    """
    by_name = {obj["object_name"].casefold(): obj for obj in objects}
    # чанки — ОРИГИНАЛЬНЫМИ именами: COLLATE NOCASE сворачивает только
    # ASCII, кириллический casefold-ключ в IN не матчится; результат
    # возвращается в by_name через python-casefold ключ
    names = [obj["object_name"] for obj in objects]

    attrs_by_obj: dict[str, list[dict]] = {}
    for chunk in _chunks(names):
        placeholders = ",".join("?" for _ in chunk)
        for row in reader._rows(
                f"""SELECT object_name, attr_kind, ts_name
                    FROM object_attributes
                    WHERE object_name COLLATE NOCASE IN ({placeholders})""",
                tuple(chunk)):
            attrs_by_obj.setdefault(
                row["object_name"].casefold(), []).append(row)

    methods_by_obj: dict[str, list[dict]] = {}
    totals_by_obj: dict[str, int] = {}
    for chunk in _chunks(names):
        placeholders = ",".join("?" for _ in chunk)
        for row in reader._rows(
                f"""SELECT mo.object_name AS obj_name, m.name, m.type,
                           m.is_export, m.line, m.end_line
                    FROM methods m JOIN modules mo ON mo.id = m.module_id
                    WHERE mo.object_name COLLATE NOCASE IN ({placeholders})
                    ORDER BY m.line""", tuple(chunk)):
            key = row["obj_name"].casefold()
            bucket = methods_by_obj.setdefault(key, [])
            if len(bucket) < DETAIL_METHODS_CAP:
                bucket.append(row)          # список — первые 200 по line
            totals_by_obj[key] = totals_by_obj.get(key, 0) + 1

    help_by_obj: dict[str, list[dict]] = {}
    for row in reader._rows(
            "SELECT object_name, source_id, path, title FROM help_pages"):
        help_by_obj.setdefault((row["object_name"] or "").casefold(),
                               []).append(row)

    for key, obj in by_name.items():
        attrs = attrs_by_obj.get(key, [])
        obj["attributes_total"] = len(attrs)
        obj["attributes_kinds"] = sorted({a["attr_kind"] for a in attrs})
        methods = methods_by_obj.get(key, [])
        obj["methods_total"] = totals_by_obj.get(key, 0)
        entry_points = sorted({
            m["name"] for m in methods
            if m["is_export"] or m["name"] in ("ОбработкаПроведения",
                                               "ПриЗаписи", "ПередЗаписью",
                                               "ПриСозданииНаСервере")})
        obj["entry_points"] = entry_points
        method_names = {m["name"] for m in methods}
        if method_names:
            placeholders = ",".join("?" for _ in method_names)
            hot = reader._rows(
                f"""SELECT callee_name, COUNT(*) AS n FROM calls
                    WHERE callee_name COLLATE NOCASE IN ({placeholders})
                    GROUP BY callee_name ORDER BY n DESC LIMIT 5""",
                tuple(sorted(method_names)))
            obj["called_from_outside"] = [h["callee_name"] for h in hot]
        else:
            obj["called_from_outside"] = []
        obj["help"] = [{"source": h["source_id"], "path": h["path"],
                        "title": h["title"]}
                       for h in help_by_obj.get(key, [])[:10]]


def build_docs(cid: str = "") -> dict:
    """Сгенерировать детерминированный каркас досье из индекса (Т4).

    Перезаписывает dossier.md; файлы-оверлеи владельца (notes-*.md) не
    трогаются. Возвращает отчёт {path, objects_total, sections, ...} или
    {"error": ...}.
    """
    try:
        entry = _entry(cid)
    except ValueError as exc:
        return {"error": str(exc)}
    db = Path(entry.get("index_db") or "")
    if not db.exists():
        return {"error": "индекс не собран — сначала пересоберите"}
    try:
        reader = GyrReader(db)
    except Exception as exc:
        return {"error": f"индекс не читается: {exc}"}
    try:
        objects = _object_catalog(reader)
        _fill_details(reader, objects)  # compass-9cw: батчи вместо N×точечных
        info = entry.get("config_info") or {}
        lines: list[str] = []
        lines.append(f"# Досье: {entry.get('display_name') or entry['id']}")
        meta = []
        if info.get("Name"):
            meta.append(f"{info['Name']} {info.get('Version', '')}".strip())
        meta.append(f"ID: {entry['id']}")
        from ..workspace import datetime_utc
        lines.append("")
        lines.append(f"*Каркас сгенерирован из индекса {datetime_utc()}. "
                     "Нарратив LLM помечается отдельно; заметки владельца — "
                     "файлы notes-*.md, регенерация их не затирает.*")
        lines.append("")
        sections = []
        by_sub: dict[str, list[dict]] = {}
        rest: list[dict] = []
        for obj in objects:
            subs = obj.get("subsystems") or []
            if subs:
                for s in subs:
                    by_sub.setdefault(s, []).append(obj)
            else:
                rest.append(obj)
        for sub, objs in sorted(by_sub.items()):
            lines.append(f"## Подсистема: {sub}")
            sections.append(f"Подсистема: {sub}")
            lines.append("")
            for obj in objs:
                lines += _render_object(obj)
            lines.append("")
        if rest:
            lines.append("## Вне подсистем")
            sections.append("Вне подсистем")
            lines.append("")
            for obj in rest:
                lines += _render_object(obj)
            lines.append("")
        help_total = sum(len(o.get("help") or []) for o in objects)
        lines.append("---")
        lines.append("")
        lines.append(f"*Итого: объектов {len(objects)}, точек входа "
                     f"{sum(len(o['entry_points']) for o in objects)}, "
                     f"справочных страниц {help_total}.*")
        out_dir = _docs_dir(entry["id"])
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / DOSSIER_FILE
        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, target)
        return {"written": str(target), "objects_total": len(objects),
                "sections": sections, "help_pages_total": help_total}
    finally:
        reader.close()


def _render_object(obj: dict[str, Any]) -> list[str]:
    """Объект → строки markdown каркаса."""
    lines = []
    lines.append(f"### {obj['category']}.{obj['object_name']}"
                 + (f" ({obj['synonym']})" if obj["synonym"] else ""))
    lines.append("")
    facts = []
    facts.append(f"реквизитов: {obj['attributes_total']}")
    if obj["attributes_kinds"]:
        facts.append("виды: " + ", ".join(obj["attributes_kinds"]))
    facts.append(f"методов: {obj['methods_total']}")
    if obj["entry_points"]:
        facts.append("точки входа: " + ", ".join(obj["entry_points"]))
    if obj["called_from_outside"]:
        facts.append("касаются извне: " + ", ".join(obj["called_from_outside"]))
    if obj["help"]:
        facts.append("справка: " + "; ".join(
            f"{h['path']} ({h['source']})" for h in obj["help"]))
    for fact in facts:
        lines.append(f"- {fact}")
    lines.append("")
    return lines


def get_docs(cid: str = "", section: str = "") -> dict:
    """Прочитать досье конфигурации: каркас (dossier.md) целиком или
    раздел (подстрока заголовка «## …»), плюс файлы-оверлеи владельца."""
    try:
        entry = _entry(cid)
    except ValueError as exc:
        return {"error": str(exc)}
    docs_dir = _docs_dir(entry["id"])
    dossier = docs_dir / DOSSIER_FILE
    if not dossier.exists():
        return {"error": "досье не собрано — вызовите build_docs "
                "(генерируется автоматически при пересборке индекса)"}
    text = dossier.read_text(encoding="utf-8")
    if section:
        lines = text.splitlines()
        picked: list[str] = []
        capturing = False
        for ln in lines:
            if ln.startswith("## "):
                capturing = section.casefold() in ln.casefold()
                if capturing:
                    picked.append(ln)
                continue
            if capturing:
                picked.append(ln)
        body = "\n".join(picked).strip()
        if not body:
            return {"error": f"раздел «{section}» не найден; "
                    "секции — get_docs без параметра"}
        return {"section": section, "text": body}
    overlays = sorted(p.name for p in docs_dir.glob(OVERLAY_PREFIX + "*.md"))
    return {"file": str(dossier), "text": text, "overlays": overlays}


def search_dossier(query: str, limit: int = 20) -> list | dict:
    """Поиск по досье и заметкам владельца (файлы notes-*.md): подстрока,
    регистронезависимо; возврат {file, section, line, text}."""
    query = (query or "").strip()
    if not query:
        return {"error": "пустой запрос"}
    from ..context import active_config_id
    target = active_config_id()
    if not target:
        return {"error": "активная конфигурация не выбрана"}
    docs_dir = _docs_dir(target)
    if not docs_dir.is_dir():
        return {"error": "досье не собрано — вызовите build_docs"}
    needle = query.casefold()
    out: list[dict] = []
    for path in sorted(docs_dir.glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        current = ""
        for i, ln in enumerate(lines, start=1):
            if ln.startswith("## "):
                current = ln[3:].strip()
            if needle in ln.casefold():
                out.append({"file": path.name, "section": current,
                            "line": i, "text": ln.strip()[:200]})
                if len(out) >= max(1, min(int(limit or 20), 100)):
                    return out
    if not out:
        return [{"_no_matches": True,
                 "hint": "в досье и заметках совпадений нет; проверьте "
                         "search_help по справке конфигурации"}]
    return out
