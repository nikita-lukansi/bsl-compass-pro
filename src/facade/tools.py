# -*- coding: utf-8 -*-
"""Тулы разведки поверх index.db движка gyrfalcon (все — один читатель).

Каждая функция возвращает dict/list для JSON-ответа; честные маркеры:
error/info вместо пустых «не найдено». Тонкие — вся логика в SQL читателя
и лёгкой сборке ответов.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..config import config
from ..parsers.skd import parse_skd
from ..recon.bodies import bodies_in_index
from ..recon.gyr_reader import CALLEE_LAST, GyrReader
# compass-c6z (§7 №1-2): бриф из ядра — eager-импорт слоя рантайма разорван
from ..runtime_mode import runtime_brief as _runtime_brief

# Предопределённые обработчики записи/проведения (для triggers_on_write)
WRITE_HANDLERS = (
    "ПередЗаписью", "ПриЗаписи", "ПередУдалением", "ПриУдалении",
    "ОбработкаПроведения", "ОбработкаУдаленияПроведения",
    "ПередЗаписьюНабораЗаписей", "ПриЗаписиНабораЗаписей",
    "ПриКопировании", "ПриСозданииНаСервере",
)

# compass-g2r: платформенные стандартные реквизиты по виду объекта
# (category классификатора). В object_attributes они не попадают, но
# есть у КАЖДОГО объекта вида — «доказанное» holds:false на них
# недопустимо (стресс w2d: «у документа нет номера»). Ключ — category.
STANDARD_ATTRIBUTES = {
    "Documents": {"Номер", "Дата", "ПометкаУдаления", "Проведен", "Ссылка"},
    "Catalogs": {"Код", "Наименование", "Родитель", "ЭтоГруппа",
                 "ПометкаУдаления", "Ссылка"},
}
# регистронезависимое сравнение — casefold один раз на старте
_STANDARD_ATTRS_CF = {kind: {n.casefold() for n in names}
                      for kind, names in STANDARD_ATTRIBUTES.items()}

# Русские префиксы видов метаданных: индекс хранит object_name БЕЗ префикса
_TYPE_PREFIXES = (
    "Справочник", "Документ", "ПланСчетов", "ПланВидовХарактеристик",
    "ПланВидовРасчета", "ПланОбмена", "РегистрНакопления", "РегистрСведений",
    "РегистрБухгалтерии", "РегистрРасчета", "Перечисление", "БизнесПроцесс",
    "Задача", "Обработка", "Отчет",
)


def _canonical_object_name(object_name: str) -> str:
    """«Справочник.ВидыЗапасов» → «ВидыЗапасов» (индекс хранит имена без
    префикса вида метаданных). Уже чистое имя возвращается как есть."""
    name = (object_name or "").strip()
    for prefix in _TYPE_PREFIXES:
        if name.lower().startswith(prefix.lower() + "."):
            return name[len(prefix) + 1:]
    return name


_reader: GyrReader | None = None
_reader_db = None

# F4/kwn: русские префиксы видов → каталоги выгрузки (для module_hint)
_HINT_DIRS = {"Справочник": "Catalogs", "Документ": "Documents",
              "Обработка": "DataProcessors", "Отчет": "Reports",
              "РегистрНакопления": "AccumulationRegisters",
              "РегистрСведений": "InformationRegisters",
              "РегистрБухгалтерии": "AccountingRegisters",
              "РегистрРасчета": "CalculationRegisters",
              "Перечисление": "Enums", "ПланСчетов": "ChartsOfAccounts",
              "ПланВидовХарактеристик": "ChartsOfCharacteristicTypes",
              "ПланВидовРасчета": "ChartsOfCalculationTypes",
              "ПланОбмена": "ExchangePlans",
              "БизнесПроцесс": "BusinessProcesses", "Задача": "Tasks",
              "ОбщийМодуль": "CommonModules"}


# compass-yho (обратка №5): сегмент «.Форма.<Имя>[/Вариант]» в module_hint →
# путь формы в выгрузке. Раскладка сверена с classify.rs vendored gyrfalcon:
# Catalogs/Имя/Forms/ФормаСписка/Ext/Form/Module.bsl (module_type=Module,
# is_form=1); вариант формы — следующий сегмент пути через слэш.
_FORM_HINT_RE = re.compile(
    r"^(?P<object>.+?)\.Форма\.(?P<form>[^.]+(?:/[^.]+)?)$", re.IGNORECASE)


def _module_hint_for_search(module_hint: str | None) -> str:
    """Подсказка модуля → фрагмент rel_path (F4/compass-kwn).

    «Справочник.Имя» → «Catalogs/Имя» (имя каталога выгрузки — LIKE
    матчится), «Документ.Имя» → «Documents/Имя» и т.д.; без префикса
    вида подсказка возвращается как есть (англ. фрагмент пути или имя
    объекта — rel_path их содержит).

    compass-yho: сегмент «.Форма.<Имя>[/Вариант]» → путь формы
    «<Каталог>/<Объект>/Forms/<Имя>[/Вариант]» — раньше такой хинт не
    находил модуль формы.
    """
    hint = (module_hint or "").strip()
    if not hint:
        return hint
    for ru, en in _HINT_DIRS.items():
        if hint.casefold().startswith(ru.casefold() + "."):
            rest = hint[len(ru) + 1:].strip()
            m = _FORM_HINT_RE.fullmatch(rest)
            if m:
                return (f"{en}/{m.group('object').strip()}"
                        f"/Forms/{m.group('form').strip()}")
            return f"{en}/{rest}"
    return hint


# Вид метаданных (рус./англ.) → значение колонки category индекса:
# category = первый сегмент rel_path выгрузки (classify.rs:39), у
# object_attributes — тот же английский каталог (meta.rs:230-236)
_KIND_CATEGORY_ALIASES = {
    **{ru.casefold(): en for ru, en in _HINT_DIRS.items()},
    **{en.casefold(): en for en in _HINT_DIRS.values()},
}

# category (каталог выгрузки) → единственное число вида в object_ref
# подсистем (subsystem_content.object_ref = «Document.ДокЗаказ» из XML
# состава, strip_cfg; build.rs:715-727)
_CATEGORY_SINGULAR = {
    "Catalogs": "Catalog", "Documents": "Document",
    "ChartsOfAccounts": "ChartOfAccounts",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
    "AccountingRegisters": "AccountingRegister",
    "InformationRegisters": "InformationRegister",
    "AccumulationRegisters": "AccumulationRegister",
    "CalculationRegisters": "CalculationRegister",
    "ExchangePlans": "ExchangePlan", "Enums": "Enum",
    "BusinessProcesses": "BusinessProcess", "Tasks": "Task",
    "DataProcessors": "DataProcessor", "Reports": "Report",
    "CommonModules": "CommonModule",
}


def _resolve_kind_query(object_name: str) -> tuple[str, str | None]:
    """«ПланСчетов.Хозрасчетный» / «ChartsOfAccounts.Хозрасчетный» →
    («Хозрасчетный», «ChartsOfAccounts»); голое имя → (имя, None).

    compass-1hh: object_name в индексе хранится без вида — план счетов
    и регистр бухгалтерии неразличимы; вид из запроса задаёт фильтр по
    category. Неизвестный префикс (не вид метаданных) — часть имени:
    возвращается как есть.
    """
    name = (object_name or "").strip()
    if "." not in name:
        return name, None
    prefix, rest = name.split(".", 1)
    category = _KIND_CATEGORY_ALIASES.get(prefix.strip().casefold())
    if category is None or not rest.strip():
        return name, None
    return rest.strip(), category


def _categories_of(r: GyrReader, name: str) -> list[str]:
    """Виды (category), в которых индекс знает объект с этим именем —
    реквизиты + модули (1hh: план счетов и его регистр дают две строки)."""
    rows = r._rows(
        """SELECT DISTINCT category FROM (
               SELECT category FROM object_attributes
                WHERE object_name = ? COLLATE NOCASE
                  AND category IS NOT NULL
               UNION
               SELECT category FROM modules
                WHERE object_name = ? COLLATE NOCASE
                  AND category IS NOT NULL
           ) ORDER BY 1""",
        (name, name))
    return [x["category"] for x in rows]


def active_index_db():
    """Индекс активной конфигурации ТЕКУЩЕГО КОНТЕКСТА (чат агента/панель).

    Приоритет: env COMPASS_INDEX_DB (режим разработчика — устраняет
    рассинхрон с config.py, P2/A12) → привязка чата (sessions.json) →
    default_config_id (settings.json) → None. Файл проверяется на
    существование.
    """
    import os

    env = os.getenv("COMPASS_INDEX_DB")
    if env:
        p = Path(env)
        return p if p.exists() else None
    from ..registry import get_config
    from ..context import active_config_id

    cid = active_config_id()
    if not cid:
        return None
    entry = get_config(cid)
    if not entry:
        return None
    p = Path(entry["index_db"])
    return p if p.exists() else None


def reader() -> GyrReader | None:
    """Ленивый читатель активной конфигурации контекста; None, если
    конфигурация не выбрана или индекса нет. При смене пути (переключение
    конфигурации/сборка) пересоздаётся сам — рестарт не нужен."""
    global _reader, _reader_db
    db = active_index_db()
    if db is None:
        return None
    if _reader is None or _reader_db != db:
        if _reader is not None:
            _reader.close()
        _reader = GyrReader(db)
        _reader_db = db
    return _reader


def reset_reader() -> None:
    """Сбросить кэш читателя — после пересборки индекса в тот же путь.

    Старое соединение SQLite держит старый inode пересобранного файла;
    без сброса тулы продолжат читать устаревший индекс до рестарта.
    """
    global _reader, _reader_db
    if _reader is not None:
        try:
            _reader.close()
        except Exception:
            pass  # закрытие мёртвого соединения не должно ронять сброс
    _reader = None
    _reader_db = None


def _no_index(reason: str = "") -> dict:
    """Честная ошибка «индекса нет» с различимыми подсказками (F6/compass-9n8).

    Ветки: no_config — конфигурация не выбрана (config_list → config_use);
    building — идёт сборка индекса выбранной конфигурации (compass-epw:
    честный статус вместо «не собран» — на первой сборке файла ещё нет);
    not_built — выбрана, но индекс не собран (панель / POST /reindex);
    env_missing — COMPASS_INDEX_DB задан, но файла нет (dev-ветка — env
    упоминается только здесь). reason="" — автодетект по env/реестру.
    Пересборка при существующем индексе сюда не попадает: старый файл
    живёт до конца сборки (атомарный rename, PERF-4), тулы читают его.
    """
    import os

    from ..context import active_config_id
    from ..registry import get_config

    env = os.getenv("COMPASS_INDEX_DB", "")
    if not reason or reason == "auto":
        if env:
            reason = "env_missing"
        else:
            try:
                cid = active_config_id()
                entry = get_config(cid) if cid else None
                if entry is None:
                    reason = "no_config"
                else:
                    from ..state import BUILD_STATE
                    reason = ("building" if BUILD_STATE.get("building")
                              else "not_built")
            except Exception:
                reason = "no_config"
    texts = {
        "no_config": ("конфигурация не выбрана: config_list → config_use "
                      "(или выберите в панели), затем повторите"),
        "building": ("идёт сборка индекса выбранной конфигурации — "
                     "повторите после её завершения (прогресс — панель, "
                     "/api/status.build)"),
        "not_built": ("индекс не собран для выбранной конфигурации: "
                      "соберите через панель или POST /reindex, "
                      "затем повторите"),
        "env_missing": (f"COMPASS_INDEX_DB={env} указывает на "
                        "несуществующий файл — проверьте переменную "
                        "окружения или уберите её"),
    }
    return {"error": texts.get(reason, texts["no_config"])}


def _overrides_badge(r: GyrReader, method: str | None = None,
                     object_name: str | None = None) -> dict | None:
    """Бейдж «метод/объект перехватывается расширениями» (Т3-N): {count,
    instead, hint: effective_method} при наличии перехватов, иначе None.
    instead — число аннотаций, содержащих «Вместо» (толерантно к формату)."""
    if method:
        where, param = "target_method = ? COLLATE NOCASE", method
    elif object_name:
        where, param = "object_name = ? COLLATE NOCASE", object_name
    else:
        return None
    rows = r._rows(
        f"SELECT annotation FROM extension_overrides WHERE {where}",
        (param,))
    if not rows:
        return None
    return {"count": len(rows),
            "instead": sum(1 for x in rows
                           if "Вместо" in (x["annotation"] or "")),
            "hint": "effective_method"}


# ------------------------------------------------------------------ методы

def search_function(query: str, limit: int = 10) -> list[dict[str, Any]] | dict:
    r = reader()
    if r is None:
        return _no_index()
    rows = r.find_methods(query, limit=limit)
    if not rows:
        return [{"_no_matches": True, "query": query,
                 "hint": "попробуй другое написание или часть имени"}]
    return rows


def read_function(name: str, module_hint: str | None = None,
                  include_body: bool = True) -> dict[str, Any] | dict:
    """Метод + тело (из исходника) + вызовы/вызывающие одним вызовом.

    module_hint — имя объекта или фрагмент пути, с префиксом вида или
    без: «Документ.РеализацияТоваровУслуг» транслируется в
    «Documents/РеализацияТоваровУслуг» (F4/kwn); старые англ. хинты
    работают как раньше. Каждый ответ несёт config активной конфигурации
    (compass-yho), когда она выбрана."""
    r = reader()
    if r is None:
        return _echo_config(_no_index())
    rows = r.get_method(name, _module_hint_for_search(module_hint))
    if not rows:
        return _echo_config({"error": f"метод '{name}' не найден"})
    m = rows[0]
    out: dict[str, Any] = {
        "name": m["name"], "type": m["type"], "params": m["params"],
        "is_export": bool(m["is_export"]),
        "module_path": m["rel_path"], "module_type": m["module_type"],
        "line_start": m["line"], "line_end": m["end_line"],
        "ambiguous_total": len(rows),
    }
    if include_body:
        if config.source_root and config.source_root.exists():
            out["body"] = r.read_method_source(
                m["rel_path"], m["line"], m["end_line"], config.source_root
            )
        else:
            out["body_info"] = "COMPASS_SOURCE_ROOT не задан — тело не прочитано"
    out["callees"] = r.callees(m["id"])[:50]
    out["callers"] = r.callers(name, limit=30)
    badge = _overrides_badge(r, method=name)
    if badge:
        out["overrides"] = badge
    return _echo_config(out)


# ------------------------------------------------------- модуль и каталог (Волна 1)

def read_module(object_name: str, module_hint: str = "",
                include_body: bool = False, line_start: int = 0,
                line_end: int = 0) -> dict | list:
    """Модуль объекта целиком: путь, заголовок, области, методы с границами.

    Дополнение read_function (Волна 1 п.2): агент видит структуру модуля,
    не только один метод. Объект может иметь несколько модулей
    (Объект/Менеджер/Формы) — без module_hint возвращаются все; тело
    читается только для ОДНОГО найденного модуля по запросу
    (include_body, строки line_start..line_end; 0 = весь модуль по
    границам методов).
    """
    r = reader()
    if r is None:
        return _no_index()
    name = _canonical_object_name(object_name)
    mods = r._rows(
        """SELECT m.id, m.rel_path, m.module_type, m.form_name, h.header_comment
           FROM modules m
           LEFT JOIN module_headers h ON h.module_id = m.id
           WHERE m.object_name = ? COLLATE NOCASE ORDER BY m.id""",
        (name,))
    hint = _module_hint_for_search(module_hint).lower()
    if hint:
        mods = [m for m in mods
                if hint in (m["rel_path"] or "").lower()
                or hint in (m["module_type"] or "").lower()
                or hint in (m["form_name"] or "").lower()]
    if not mods:
        return {"error": f"модули объекта '{object_name}' не найдены",
                "hint": "проверьте имя (search_function) или module_hint "
                        "(ObjectModule/ManagerModule/имя формы)"}
    out = []
    for m in mods:
        regions = r._rows(
            "SELECT name, line, end_line FROM regions WHERE module_id = ? "
            "ORDER BY line", (m["id"],))
        methods = r._rows(
            "SELECT name, type, is_export, params, line, end_line, loc "
            "FROM methods WHERE module_id = ? ORDER BY line", (m["id"],))
        item = {
            "rel_path": m["rel_path"], "module_type": m["module_type"],
            "form_name": m["form_name"],
            "header_comment": m["header_comment"] or "",
            "regions": regions, "methods": methods,
            "methods_total": len(methods),
        }
        if include_body and len(mods) == 1:
            if config.source_root and config.source_root.exists():
                start = max(1, int(line_start or 0))
                end = int(line_end or 0) or max(
                    [x["end_line"] or 0 for x in methods] + [0])
                if end < start:
                    end = start
                item["body"] = r.read_method_source(
                    m["rel_path"], start, end, config.source_root)
                item["body_lines"] = [start, end]
            else:
                item["body_info"] = ("COMPASS_SOURCE_ROOT не задан — "
                                     "тело не прочитано")
        elif include_body and len(mods) > 1:
            item["body_info"] = ("несколько модулей — уточните module_hint, "
                                 "тело читается для одного модуля")
        out.append(item)
    return out[0] if len(out) == 1 else out


def list_objects(category: str = "", subsystem: str = "",
                 search: str = "", limit: int = 100) -> list | dict:
    """Каталог объектов индекса (Волна 1 п.3): фильтры по типу категории,
    подсистеме и поиску по имени/синониму (FTS trigram — запрос ≥ 3 символов,
    короче — LIKE).

    Источник каталога — object_synonyms (объекты с синонимами;
    objects_fts построен из той же таблицы). Подсистема —
    subsystem_content (object_ref вида «Document.ДокЗаказ»).
    """
    r = reader()
    if r is None:
        return _no_index()
    where: list[str] = []
    params: list = []
    search = (search or "").strip()
    if search:
        if len(search) >= 3:
            where.append(
                "os.id IN (SELECT rowid FROM objects_fts WHERE objects_fts "
                "MATCH ?)")
            params.append(search)
        else:
            where.append("(os.object_name LIKE ? OR os.synonym LIKE ?)")
            params.extend((f"%{search}%", f"%{search}%"))
    category = (category or "").strip()
    if category:
        where.append("os.category = ? COLLATE NOCASE")
        params.append(category)
    subsystem = (subsystem or "").strip()
    joins = ""
    if subsystem:
        joins = ("JOIN subsystem_content sc ON (sc.object_ref LIKE '%.' || "
                 "os.object_name OR sc.object_ref = os.object_name)")
        where.append("sc.subsystem_name = ? COLLATE NOCASE")
        params.append(subsystem)
    sql = ("SELECT os.object_name, os.category, os.synonym "
           "FROM object_synonyms os " + joins
           + ("WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY os.category, os.object_name LIMIT ?")
    params.append(max(1, min(int(limit or 100), 500)))
    rows = r._rows(sql, tuple(params))
    if not rows:
        return [{"_no_matches": True,
                 "hint": "объекты не найдены; каталог содержит объекты с "
                         "синонимами — проверьте категорию/подсистему или "
                         "ослабьте поиск"}]
    return rows


# ------------------------------------------------------- запись и проведение

def triggers_on_write(object_name: str) -> dict[str, Any] | dict:
    """Что срабатывает при записи: обработчики объекта + подписки + движения."""
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    handlers = r._rows(
        """SELECT m.name, m.params, m.line, mo.rel_path, mo.module_type
           FROM methods m JOIN modules mo ON mo.id = m.module_id
           WHERE mo.object_name = ? COLLATE NOCASE
             AND m.name IN ({placeholders})""".format(
            placeholders=",".join("?" * len(WRITE_HANDLERS))),
        (object_name, *WRITE_HANDLERS),
    )
    out = {
        "object": object_name,
        "handlers": handlers,
        "subscriptions": r.event_subscriptions(object_name),
        "movements": r.movements(object_name),
    }
    badge = _overrides_badge(r, object_name=object_name)
    if badge:
        out["overrides"] = badge
    return out


# -------------------------------------------------------------- данные/влияние

def who_touches(object_name: str, limit: int = 50) -> dict[str, Any] | dict:
    """Кто трогает данные объекта в коде (data-lineage по code_usages),
    регистраторы регистров (если объект — регистр)."""
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    usages = r.code_usages(object_name, limit=limit)
    registrars = [
        row for row in r._rows(
            "SELECT DISTINCT document_name FROM register_movements "
            "WHERE register_name = ? COLLATE NOCASE", (object_name,))
    ]
    out: dict[str, Any] = {"object": object_name, "code_usages": usages}
    if registrars:
        out["registrars"] = [d["document_name"] for d in registrars]
    badge = _overrides_badge(r, object_name=object_name)
    if badge:
        out["overrides"] = badge
    if not usages and not registrars:
        out["_no_matches"] = True
        out["hint"] = "обращений не найдено; проверь имя через search_function"
    return out


def find_references_to(object_name: str, limit: int = 100) -> list[dict] | dict:
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    return r.references_to(object_name, limit=limit)


def find_in_forms(what: str, object_name: str | None = None,
                  limit: int = 30) -> list[dict] | dict:
    object_name = _canonical_object_name(object_name or "")
    r = reader()
    if r is None:
        return _no_index()
    rows = r.find_in_forms(what, limit=limit)
    if object_name:
        rows = [x for x in rows if x["object_name"].lower() == object_name.lower()]
        # Т3-N: list-ответ не расширяется полем — бейдж идёт служебным
        # элементом-маркером в конце (как _no_matches/_truncated)
        badge = _overrides_badge(r, object_name=object_name)
        if badge:
            rows = [*rows, {"_overrides": badge}]
    return rows


# ------------------------------------------------------------------ метаданные

def _attr_filter_re(pattern: str) -> re.Pattern | None:
    """Шаблон фильтра реквизитов (compass-93j): «*» — любая подстрока,
    «?» — один символ; без масок — подстрока. Регистронезависимо;
    None — фильтр не задан."""
    if not pattern or not pattern.strip():
        return None
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.IGNORECASE)


def object_overview(object_name: str, attr_filter: str = "",
                    attr_page: int = 1,
                    attr_page_size: int = 50) -> dict[str, Any] | dict:
    """Всё по объекту одним вызовом (аналог Community object_overview).

    compass-1hh: object_name в индексе хранится без вида — имя, которое
    носят разные виды метаданных (ПланСчетов.Хозрасчетный и
    РегистрБухгалтерии.Хозрасчетный), даёт честную дизамбигуацию
    («_ambiguous»: True + категории-кандидаты), атрибуты не смешиваются;
    запрос с видом («ПланСчетов.Хозрасчетный») фильтрует по category и
    возвращает её в ответе.

    compass-93j: секция attributes поддерживает фильтр и пагинацию.
    attr_filter — подстрока или маска («*» = любая подстрока, «?» = один
    символ; регистронезависимо, матчится на имя и синоним реквизита);
    attr_page — номер страницы (1-based), attr_page_size — размер
    (1..1000; дефолт 1×50 — прежний срез первых 50 по алфавиту).
    attributes_total — счётчик ПОСЛЕ фильтра; attributes_kinds — точные
    счётчики по отфильтрованным; table_sections (ТЧ) — всегда по всему
    объекту. Добор — attr_page (или verify_field по конкретному имени).
    compass-yho ②: для справочника — owners (владельцы), для любого вида —
    standard_attributes (имена, отсутствующие в индексе) — из XML выгрузки.
    Каждый ответ несёт config активной конфигурации (compass-yho),
    когда она выбрана.
    """
    bare, category = _resolve_kind_query(object_name)
    r = reader()
    if r is None:
        return _echo_config(_no_index())
    cats = _categories_of(r, bare)
    if category is None and len(cats) > 1:
        return _echo_config({"_ambiguous": True, "object": bare,
                             "categories": [{"category": c,
                                             "query": f"{c}.{bare}"}
                                            for c in cats],
                             "hint": ("это имя носят разные виды метаданных — "
                                      "уточните запрос видом, например "
                                      "object_overview('" + cats[0] + "."
                                      + bare + "'); без вида атрибуты "
                                      "смешались бы")})
    kind = category or (cats[0] if len(cats) == 1 else None)
    attrs = r.object_attributes(bare)
    if kind:
        attrs = [a for a in attrs if (a.get("category") or "") == kind]
    subs = r.subsystems_of(bare)
    if kind:
        # object_ref = «ВидЕдинств.Имя» — фильтр по виду снимает дубли
        # подсистем от второго объекта с тем же именем (1hh)
        singular = _CATEGORY_SINGULAR.get(kind)
        if singular:
            subs = [s for s in subs
                    if s["object_ref"] == f"{singular}.{bare}"]
    seen: set = []
    subs_dedup: list[dict] = []
    for s in subs:
        key = (s["subsystem_name"], s["subsystem_synonym"])
        if key not in seen:
            seen.append(key)
            subs_dedup.append(s)
    forms = r.forms_of(bare)
    refs = r.references_to(bare, limit=5)
    refs_total = r._rows(
        "SELECT COUNT(*) AS n FROM metadata_references WHERE ref_object LIKE ?",
        (f"%{bare}%",))[0]["n"]
    enum = r.enum_values(bare)
    top_sql = """SELECT m.name, m.params, m.is_export, m.loc
           FROM methods m JOIN modules mo ON mo.id = m.module_id
           WHERE mo.object_name = ? COLLATE NOCASE"""
    top_params: list[Any] = [bare]
    if kind:
        top_sql += " AND mo.category = ? COLLATE NOCASE"
        top_params.append(kind)
    top_sql += " ORDER BY m.is_export DESC, m.loc DESC LIMIT 5"
    top = r._rows(top_sql, tuple(top_params))
    ts_groups: dict[str, list[dict]] = {}
    for a in attrs:
        ts = a.get("ts_name")
        if ts:
            ts_groups.setdefault(ts, []).append(
                {"name": a["attr_name"], "type": a["attr_type"]})
    if not attrs and not top and enum is None:
        # перечисления без методов/реквизитов не «не найдены» — у них
        # enum_values (F3); ранний выход только если и этого нет
        return _echo_config({"_no_matches": True, "object": object_name,
                             "hint": "объект не найден; проверь имя через "
                                     "search_function; попробуй имя без "
                                     "префикса вида («Справочник.», "
                                     "«Документ.»…)"})
    # compass-93j: фильтр (подстрока/маска) и пагинация attributes;
    # дефолт (без фильтра, страница 1×50) — прежний срез первых 50
    attr_re = _attr_filter_re(attr_filter)
    matched = attrs
    if attr_re is not None:
        matched = [a for a in attrs
                   if attr_re.search(a.get("attr_name") or "")
                   or attr_re.search(a.get("attr_synonym") or "")]
    page = max(1, int(attr_page or 1))
    page_size = min(max(1, int(attr_page_size or 50)), 1000)
    pages = max(1, -(-len(matched) // page_size))   # ceil-деление
    page = min(page, pages)
    out = {
        "object": bare,
        **({"category": kind} if kind else {}),
        "attributes": matched[(page - 1) * page_size:page * page_size],
        "attributes_total": len(matched),
        "attributes_kinds": {k: sum(1 for a in matched
                                    if a["attr_kind"] == k)
                             for k in {a["attr_kind"] for a in matched}},
        "attributes_page": {"page": page, "pages": pages,
                            "page_size": page_size},
        "table_sections": [{"name": ts, "columns": cols}
                           for ts, cols in ts_groups.items()],
        "table_sections_total": len(ts_groups),
        "subsystems": [s["subsystem_name"] for s in subs_dedup],
        "forms": forms,
        "referenced_by_total": refs_total,
        "referenced_by_sample": refs,
        **({"enum_values": {**enum,
                            "note": ("перечисление без значений"
                                     if not enum["values"] else "")}}
           if enum is not None else {}),
        "movements": r.movements(bare),
        "top_methods": top,
    }
    # compass-a49: note о срезе — только когда он есть (total > 50);
    # «первые 50 из 28» дезинформировало. compass-93j: при фильтре —
    # расширенный текст с номером страницы; не-дефолтная пагинация —
    # page-aware текст вместо «первые N» (на странице 2 это уже не «первые»)
    if attr_re is not None:
        out["attributes_note"] = (
            "страница {0} из {1} (после фильтра «{2}» осталось {3} "
            "реквизитов); kinds — точные счётчики по отфильтрованным, "
            "ТЧ — в table_sections (весь объект), точная проверка — "
            "verify_field; добор — attr_page/attr_page_size".format(
                page, pages, attr_filter.strip(), len(matched)))
    elif pages > 1 and page == 1 and page_size == 50:
        out["attributes_note"] = (
            "первые 50 из {0}; kinds — точные счётчики, ТЧ — "
            "в table_sections, точная проверка — verify_field".format(
                len(matched)))
    elif pages > 1:
        out["attributes_note"] = (
            "страница {0} из {1} (по {2}); всего {3} реквизитов; kinds — "
            "точные счётчики, ТЧ — в table_sections, точная проверка — "
            "verify_field".format(page, pages, page_size, len(matched)))
    badge = _overrides_badge(r, object_name=bare)
    if badge:
        out["overrides"] = badge
    # compass-yho ② (обратка №5): владельцы справочника и стандартные
    # реквизиты вида — из XML выгрузки (индекс их не хранит); стандартные
    # отдаются только те, кого нет среди проиндексированных атрибутов;
    # без source_root/XML секций нет — честное отсутствие
    xml_facts = _xml_kind_facts(bare, kind)
    if kind == "Catalogs" and "owners" in xml_facts:
        out["owners"] = xml_facts["owners"]
    in_index = {a.get("attr_name") for a in attrs}
    std_names = [n for n in xml_facts.get("standard_attributes", [])
                 if n not in in_index]
    if std_names:
        out["standard_attributes"] = std_names
    return _echo_config(out)


def object_rights(object_name: str) -> list[dict] | dict:
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    return r.role_rights(object_name)


def predefined_items(object_name: str, query: str = "",
                     limit: int = 100) -> list[dict] | dict:
    """Предопределённые элементы объекта (индекс: имя/код/синоним).

    Для перечислений — ЗНАЧЕНИЯ (compass-4w7.2): сборщик кладёт их в
    enum_values, не в predefined; ответ {items, kind: "enum_values",
    query, total} со значениями {name} (синонимы значений индекс не
    хранит — F3/compass-1v3). Справочники/планы — прежнее поведение
    (список item_name/item_code/item_synonym) без изменений.

    query — подстрока имени/кода/синонима (регистронезависимо);
    limit — потолок выдачи (дефолт 100; 0 — без лимита).
    Пример: predefined_items("ВидыСубконтоХозрасчетные", query="41")"""
    bare, category = _resolve_kind_query(object_name)
    r = reader()
    if r is None:
        return _echo_config(_no_index())
    q = (query or "").strip().lower()
    # ветка перечислений: вид задан («Перечисление.»/«Enums.») или имя
    # само лежит в enum_values (проба читателем — None = не перечисление)
    if category in (None, "Enums"):
        enum = r.enum_values(bare)
        if enum is not None:
            values = [{"name": v} for v in enum["values"]]
            if q:
                values = [v for v in values if q in v["name"].lower()]
            out: dict[str, Any] = {"items": values, "kind": "enum_values",
                                   "query": q, "total": len(values)}
            if limit and len(values) > limit:
                out["items"] = values[:limit]
                out["_truncated"] = True
                out["hint"] = "показаны первые {0}; уточни query".format(limit)
            return _echo_config(out)
    items = r.predefined(bare)
    if q:
        items = [i for i in items if q in (i.get("item_name") or "").lower()
                 or q in (i.get("item_code") or "").lower()
                 or q in (i.get("item_synonym") or "").lower()]
    total = len(items)
    if limit and len(items) > limit:
        items = items[:limit]
        return _echo_config([*items, {"_truncated": True, "total": total,
                                      "hint": "показаны первые {0}; уточни "
                                              "query".format(limit)}])
    return _echo_config(items)


# каталоги выгрузки, где живут Predefined.xml (порядок = приоритет поиска)
_PREDEF_DIRS = ("ChartsOfAccounts", "Catalogs", "ChartsOfCharacteristicTypes",
                "ChartsOfCalculationTypes", "ExchangePlans", "Enums")


def _xml_kind_facts(bare: str, kind: str | None) -> dict[str, Any]:
    """Владельцы и стандартные реквизиты вида из XML выгрузки (compass-yho ②,
    обратка №5): индекс их не хранит (STANDARD_ATTRIBUTES — лишь константа
    для оракулов compass-g2r, vendored-схема не расширяется).

    Источник — XML объекта <каталог вида>/<имя>.xml в доме source (path-паттерн
    как у predefined_details: там же лежит Ext/Predefined.xml). NOT Configuration.
    xml: там только состав конфигурации, владельцев/стандартных реквизитов
    нет (сверено с живой выгрузкой КА 2.5 — Owners/StandardAttributes лежат
    в Catalogs/<Имя>.xml → Properties). Возвращает {owners: [...]} для
    справочников и {standard_attributes: [имена]} — имена из атрибута
    name элемента xr:StandardAttribute; {} — честное отсутствие (нет
    source_root/файла, XML не разобрался, вид неизвестен)."""
    if not kind or not config.source_root:
        return {}
    path = Path(config.source_root) / kind / f"{bare}.xml"
    if not path.exists():
        return {}
    import xml.etree.ElementTree as Et

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    try:
        root = Et.parse(path).getroot()
    except Et.ParseError:
        return {}
    wrapper = next(iter(root), None)
    if wrapper is None:
        return {}
    props = next((c for c in wrapper if local(c.tag) == "Properties"), None)
    if props is None:
        return {}
    facts: dict[str, Any] = {}
    for node in props:
        tag = local(node.tag)
        if tag == "Owners" and kind == "Catalogs":
            facts["owners"] = [(ch.text or "").strip() for ch in node
                               if (ch.text or "").strip()]
        elif tag == "StandardAttributes":
            facts["standard_attributes"] = [
                name for name in (ch.get("name") or "" for ch in node)
                if name]
    return facts


def predefined_details(object_name: str) -> dict | dict:
    """Предопределённые элементы объекта ИЗ ВЫГРУЗКИ (Predefined.xml):
    все поля, включая табличные части (generic) — то, чего не видит
    индекс (predefined_items отдаёт только имя/код). Нужен source_root."""
    object_name = _canonical_object_name(object_name)
    # Локальный импорт — без циклических ссылок
    from ..parsers.predefined import parse_predefined
    if not config.source_root:
        return {"error": "нужен COMPASS_SOURCE_ROOT / активная конфигурация"}
    tried = []
    for folder in _PREDEF_DIRS:
        p = Path(config.source_root) / folder / object_name / "Ext" / "Predefined.xml"
        tried.append(str(p))
        if p.exists():
            items = parse_predefined(p)
            if items is None:
                return {"error": f"файл не разбирается: {p}"}
            return {"object": object_name, "path": str(p), "items": items,
                    "items_total": len(items)}
    return {"error": "Predefined.xml не найден", "tried": tried,
            "hint": "объект без предопределённых или другое имя каталога"}


def account_subaccounts(plan_name: str, code: str) -> dict | dict:
    """Субконто предопределённого счёта плана счетов (из Predefined.xml).

    Пример: account_subaccounts("Хозрасчетный", "41") — счёт по коду →
    строки ТЧ с видами субконто (имя таблицы ищется по «Субконто»).
    """
    details = predefined_details(plan_name)
    if "error" in details:
        return details
    code = (code or "").strip()
    item = next((i for i in details["items"]
                 if i["code"] == code or i["name"].lower() == code.lower()),
                None)
    if item is None:
        return {"error": f"предопределённый элемент «{code}» не найден",
                "known_codes": [i["code"] for i in details["items"][:50]]}
    out = {"plan": _canonical_object_name(plan_name), "code": item["code"],
           "name": item["name"], "description": item["description"],
           "extra": item["extra"]}
    ts_key = next((k for k in item["tables"]
                   if "субконто" in k.lower()), None)
    if ts_key:
        out["subaccounts"] = item["tables"][ts_key]
        out["subaccounts_table"] = ts_key
    else:
        out["subaccounts"] = []
        out["tables_found"] = sorted(item["tables"])
        out["note"] = ("ТЧ субконто не найдена в файле — проверьте формат "
                       "выгрузки/наличие ТЧ у этого счёта")
    return out


def entry_points() -> dict | dict:
    r = reader()
    if r is None:
        return _no_index()
    return r.entry_points()


# ------------------------------------------------------------------ подсистемы

def subsystems_overview() -> list[dict] | dict:
    r = reader()
    if r is None:
        return _no_index()
    return r._rows(
        """SELECT subsystem_name, COUNT(*) AS objects
           FROM subsystem_content GROUP BY subsystem_name
           ORDER BY objects DESC LIMIT 100""")


def subsystems_of(object_name: str) -> list[dict] | dict:
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    rows = r.subsystems_of(object_name)
    if not rows:
        return [{"_no_matches": True, "object": object_name,
                 "hint": "прямого вхождения нет (объект-сирота?); попробуй "
                         "имя без префикса вида («Справочник.», «Документ.»…)"}]
    return rows


# ------------------------------------------------------------------- оракулы

def _config_badge() -> dict[str, Any]:
    """Бейдж активной конфигурации (обратка 2026-11-15, compass-4w7.3;
    compass-yho — версия; compass-sm4 — режим): {config: {id,
    short_name, version, mode}} — вердикт
    всегда привязан к конфигурации, чей индекс проверялся. version —
    config_info.Version из Configuration.xml (как config_list, compass-4w7.4),
    None, когда не прочитана. Без активной конфигурации — пусто; cid без
    записи в реестре (битая/фиктивная привязка) — тоже пусто: бейдж без
    short_name врал бы о привязке. Проблемы реестра не роняют тул."""
    try:
        from ..registry import get_config
        cid = config.active_config_id
        entry = get_config(cid) if cid else None
    except Exception:
        return {}
    if not cid or entry is None:
        return {}
    return {"config": {"id": cid,
                       "short_name": entry.get("short_name") or "",
                       "version": (entry.get("config_info") or {})
                       .get("Version"),
                       "mode": entry.get("mode", "fast")}}


def _echo_config(result: Any) -> Any:
    """Эхо активной конфигурации в ответ тула (compass-yho, обратка №5):
    закрывает класс ошибок «не та база» — агент в чужом/холодном контексте
    видит, по какой конфигурации построен ответ. dict — ключ config
    верхнего уровня (формат 4w7.3); list — завершающим элементом-маркером
    (тот же паттерн, что _truncated); результат копируется (repomap
    кэширует списки). Без активной конфигурации — ответ без изменений."""
    badge = _config_badge()
    if not badge:
        return result
    if isinstance(result, dict):
        out = dict(result)
        out.update(badge)
        return out
    if isinstance(result, list):
        return [*result, badge]
    return result


def verify_field(object_name: str, field: str) -> dict | dict:
    """Оракул «у объекта есть реквизит / ТЧ / колонка ТЧ».

    Формат field: «Имя» (реквизит шапки или колонка ТЧ, если имя
    уникально) | «ТЧ.Колонка» (колонка конкретной табличной части).

    Вердикты (holds): True — есть (evidence); False — точно нет
    (field_not_found + nearest) — только когда ТЧ объекта индексированы
    (compass-v77: если секций ТЧ в индексе нет вообще, отсутствие поля
    недоказуемо — поле может быть неиндексированной ТЧ, напр. ВидыСубконто
    у планов счетов, и вердикт честный None not_indexed); None —
    неизвестно (object_not_found / not_indexed). Имя принимает с видом
    и без: «ПланСчетов.Хозрасчетный» фильтрует реквизиты по category
    (compass-1hh — план и регистр не смешиваются). Стандартные
    платформенные реквизиты вида (compass-g2r: Номер/Дата у документов,
    Код/Наименование у справочников…) — holds:true с evidence
    {standard: true} без обращения к индексу; not_indexed-хинт на них
    не выдаётся. Каждый ответ несёт config активной конфигурации
    (compass-4w7.3), когда она выбрана.
    """
    out = _verify_field_core(object_name, field)
    out.update(_config_badge())
    return out


def _verify_field_core(object_name: str, field: str) -> dict | dict:
    """Тело verify_field — без config-бейджа (его навешивает обёртка)."""
    bare, category = _resolve_kind_query(object_name)
    field = (field or "").strip()
    r = reader()
    if r is None:
        return _no_index()
    attrs = r.object_attributes(bare)
    if category:
        attrs = [a for a in attrs if (a.get("category") or "") == category]
    # вид в ответе: из запроса, а при голом имени — когда вид один
    # (compass-1hh); смешанное имя голым запросом категорию не получает
    cats = _categories_of(r, bare)
    effective_kind = category or (cats[0] if len(cats) == 1 else None)
    cat_note = {"category": effective_kind} if effective_kind else {}

    # 1) точное совпадение реквизита/колонки (шапка или ТЧ)
    rows = [a for a in attrs if a["attr_name"].lower() == field.lower()]
    if rows:
        return {"holds": True, "evidence": rows[0], **cat_note}

    # 1a) compass-g2r: стандартный реквизит платформы — есть у КАЖДОГО
    # объекта вида, в object_attributes не индексируется; раньше отсюда
    # выходило «доказанное» holds:false с хинтом «обе секции
    # проиндексированы». Вид — из запроса или единственный вид объекта
    # (compass-1hh); поля с точкой стандартными не бывают (шаг 1b)
    kind = category or effective_kind
    if kind and "." not in field and \
            field.casefold() in _STANDARD_ATTRS_CF.get(kind, ()):
        return {"holds": True,
                "evidence": {"attr_name": field, "standard": True,
                             "source": "platform"},
                **cat_note}

    # 1b) синтаксис «ТЧ.Колонка» (обратная связь 2026-11-15, compass-4w7.1):
    # имена реквизитов/колонок 1С точек не содержат — split по первой точке
    if "." in field:
        ts_hint, col_hint = (s.strip() for s in field.split(".", 1))
        if ts_hint and col_hint:
            ts_rows = [a for a in attrs
                       if (a.get("ts_name") or "").lower() == ts_hint.lower()]
            if ts_rows:
                col_rows = [a for a in ts_rows
                            if a["attr_name"].lower() == col_hint.lower()]
                if col_rows:
                    return {"holds": True, "evidence": col_rows[0],
                            "columns_in_section": len(ts_rows), **cat_note}
                return {"holds": False, "verdict": "field_not_found",
                        "object": bare, "field": field,
                        "nearest": [a["attr_name"] for a in ts_rows][:10],
                        "hint": f"в ТЧ «{ts_hint}» нет колонки «{col_hint}»; "
                                "ближайшие колонки секции в nearest",
                        **cat_note}
            # левая часть — не имя ТЧ: если ТЧ объекта индексированы,
            # такой секции точно нет (голые имена уже отработал шаг 1)
            if any(a.get("ts_name") for a in attrs):
                return {"holds": False, "verdict": "field_not_found",
                        "object": bare, "field": field,
                        "nearest": [],
                        "hint": f"у объекта нет ТЧ «{ts_hint}»",
                        **cat_note}

    # 2) поле — имя табличной части
    ts_columns = [a for a in attrs
                  if (a.get("ts_name") or "").lower() == field.lower()]
    if ts_columns:
        return {"holds": True, "kind": "table_section",
                "columns": [{"name": a["attr_name"], "type": a["attr_type"]}
                            for a in ts_columns], **cat_note}

    # 3) атрибуты есть, поля среди них нет
    if attrs:
        if not any(a.get("ts_name") for a in attrs):
            # v77: секции ТЧ объекта в индексе отсутствуют вовсе — объект
            # частично индексирован; «поля нет» недоказуемо (поле может
            # быть неиндексированной ТЧ — напр. ВидыСубконто плана счетов)
            nearest = [x["attr_name"] for x in attrs
                       if field.lower() in x["attr_name"].lower()][:10]
            return {"holds": None, "verdict": "not_indexed",
                    "object": bare, "field": field,
                    "nearest": nearest,
                    "hint": "секции ТЧ объекта не индексированы — "
                            "ОТРИЦАТЕЛЬНЫЙ вердикт недоказуем: поле может "
                            "быть реквизитом не в индексе или "
                            "неиндексированной ТЧ; проверь через "
                            "predefined_details или живую базу",
                    **cat_note}
        nearest = [x["attr_name"] for x in attrs
                   if field.lower() in x["attr_name"].lower()][:10]
        return {"holds": False, "verdict": "field_not_found",
                "object": bare, "field": field, "nearest": nearest,
                "hint": "поля нет среди атрибутов объекта (шапка и ТЧ — "
                        "обе секции проиндексированы); для колонок ТЧ "
                        "используй формат „ТЧ.Колонка“", **cat_note}

    # 4) атрибутов нет вовсе: объект известен по ссылкам? (не индексирован)
    known = r._rows(
        "SELECT COUNT(*) AS n FROM metadata_references "
        "WHERE ref_object LIKE ?", (f"%{bare}%",))[0]["n"]
    if known:
        return {"holds": None, "verdict": "not_indexed",
                "object": bare, "field": field,
                "hint": "атрибуты объекта не индексированы (напр., план "
                        "счетов); проверь через find_references_to, "
                        "predefined_details или живую базу"}
    return {"holds": None, "verdict": "object_not_found",
            "object": bare, "field": field,
            "hint": "объект не найден; попробуй имя без префикса вида "
                    "(«Справочник.», «Документ.»…) или search_function"}


def verify_call(caller: str, callee: str) -> dict | dict:
    """Проверка утверждения «caller вызывает callee» по рёбрам графа.

    compass-m3z: имена бывают квалифицированные («Модуль.Имя»,
    «Объект.Модуль.Имя»), а methods.name — голое имя метода; callee_name
    в calls хранится голым ИЛИ квалифицированным. Предикат зеркалит
    callers() (compass-44g): caller — по последнему сегменту имени,
    callee — точное равенство ИЛИ суффикс после последней точки
    (CALLEE_LAST по индексу idx_calls_callee_last). Старый предикат
    «имя целиком + LIKE „%.callee“» на квалифицированных именах давал
    ложные holds:false на рёбрах, которые строит call_paths/read_function.
    """
    r = reader()
    if r is None:
        return _no_index()
    caller_name = (caller or "").strip().rsplit(".", 1)[-1]
    callee_full = (callee or "").strip()
    callee_short = callee_full.rsplit(".", 1)[-1]
    rows = r._rows(
        f"""SELECT c.callee_name, c.resolution, c.confidence, m.name AS caller,
                  mo.rel_path, c.line
           FROM calls c
           JOIN methods m ON m.id = c.caller_id
           JOIN modules mo ON mo.id = m.module_id
           WHERE m.name = ? COLLATE NOCASE
             AND (c.callee_name = ? COLLATE NOCASE
                  OR {CALLEE_LAST} = ? COLLATE NOCASE)
           LIMIT 5""",
        (caller_name, callee_full, callee_short))
    if rows:
        return {"holds": True, "evidence": rows}
    return {"holds": False, "caller": caller, "callee": callee,
            "hint": "прямого ребра нет; проверь имена через read_function"}


# ---------------------------------------------------------------- расширения

def extension_map(object_name: str | None = None) -> dict | list[dict]:
    """Карта расширений: обзор (что заимствовано/перехвачено) или детали
    одного объекта — что типовое, что добавлено, что переопределено.
    Т3-O: поверх — стек активной конфигурации (эффективный порядок v0.5):
    обзор — effective_position/purpose/order_confirmed по имени;
    детали — effective_position перехватов и active/shadowed для &Вместо
    (активен верхний слой по effective_position); без стека — поля не
    добавляются, сверху order_confirmed: false."""
    r = reader()
    if r is None:
        return _no_index()
    from ..context import active_config_id
    from .. import registry as registry_mod
    # Локальный импорт — без циклических ссылок
    from .extension_tools import _kind_of
    cid = active_config_id()
    stack = registry_mod.stack_of(cid) if cid else []
    by_name = {e["name"]: e for e in stack}
    confirmed = bool(stack) and all(e.get("order_confirmed") for e in stack)
    if object_name is None:
        overview = r.extensions_overview()
        if not overview["extensions"]:
            return [{"_no_matches": True,
                     "hint": "в индексе нет расширений (дамп основной "
                             "конфигурации?) — F1 требует выгрузку расширения"}]
        for e in overview["extensions"]:
            rec = by_name.get(e["name"])
            if rec:
                e["effective_position"] = rec["effective_position"]
                e["purpose"] = rec["purpose"]
                e["in_stack"] = True
            else:
                e["in_stack"] = False
                e["hint"] = ("расширение индекса вне стека — добавьте через "
                             "extension_add (нужны позиция и назначение)")
        overview["order_confirmed"] = confirmed
        return overview
    object_name = _canonical_object_name(object_name)
    data = r.overrides_for(object_name)
    if not data["overrides"] and not data["objects"]:
        return [{"_no_matches": True, "object": object_name,
                 "hint": "объект в расширениях не найден"}]
    data["order_confirmed"] = confirmed
    if stack:
        for row in data["overrides"]:
            rec = by_name.get(row.get("extension_name"))
            if rec:
                row["effective_position"] = rec["effective_position"]
                row["kind"] = _kind_of(row.get("annotation"))
        # активный &Вместо в пределах каждой цели — верхний слой
        groups: dict[str, list[dict]] = {}
        for row in data["overrides"]:
            if (row.get("kind") == "instead"
                    and row.get("effective_position") is not None):
                groups.setdefault(row["target_method"], []).append(row)
        for group in groups.values():
            top = max(group, key=lambda x: x["effective_position"])
            top["active"] = True
            for row in group:
                if row is not top:
                    row["active"] = False
                    row["shadowed"] = True
    return data


# ------------------------------------------------------------------------ СКД

def get_skd_info(template_path: str) -> dict:
    """Схема СКД по пути (относительно COMPASS_SOURCE_ROOT или абсолютный)."""
    p = Path(template_path)
    if not p.is_absolute():
        if not config.source_root:
            return {"error": "относительный путь требует COMPASS_SOURCE_ROOT"}
        p = config.source_root / template_path
    if not p.exists():
        return {"error": f"файл не найден: {p}"}
    info = parse_skd(p)
    if info is None:
        return {"error": f"не является схемой СКД: {p}"}
    info["path"] = str(p)
    return info


# ------------------------------------------------- промоушены Волны 1 (20c)

# Бюджет узлов обхода дерева вызовов: от комбинаторного взрыва на
# «широких» методах (сотни callees на уровень); превышение — честный
# _truncated, а не тихая обрезка
CALL_PATHS_MAX_NODES = 500

# compass-cdf: именные запросы — слово/слова (буквы/цифры/подчерк/пробел)
# с опциональной префикс-звёздочкой в конце («скидк», «скидк*», «расчет
# скидк*»). Ищутся по methods_fts даже при name-only схеме; отказ «тела
# не проиндексированы» остаётся только для паттернов с код-символами
# (скобки, кавычки, операторы — по именам их не бывает)
_NAME_QUERY_RE = re.compile(r"^[\w][\w\s]*\*?$")


def code_grep(query: str, limit: int = 20,
              module_hint: str | None = None) -> list | dict:
    """Полнотекстовый поиск по коду: тела в индексе (compass-gbi), иначе именный маршрут.

    Тела залиты пост-шагом сборки (methods_bodies_fts): ответ
    {bodies_indexed: true, results: [...]}, ранжирование — rank FTS5,
    сниппет с подсветкой [..] по колонке text; строка объявления входит
    в тело — имена ищутся тем же запросом. Токенизация unicode61 — по
    словам: CamelCase-идентификатор цельный токен (подстрока внутри
    идентификатора — source_grep), слова = И, «часть*» — префикс,
    кавычки — фраза. Без тел или при COMPASS_BODIES_FTS=off — прежнее
    поведение (name, trigram — ddl.rs v0.1.20): именные запросы
    (слово/слова с опциональной «*»-префиксом — compass-cdf) ищутся по
    name; отказ «тела не проиндексированы» — только для паттернов с
    код-символами. Невалидный запрос — честная ошибка (как в
    help_search). module_hint сужает по rel_path. Каждый ответ несёт
    config активной конфигурации (compass-yho), когда она выбрана.
    """
    r = reader()
    if r is None:
        return _echo_config(_no_index())
    query = (query or "").strip()
    if bodies_in_index(r):
        return _echo_config(_code_grep_bodies(r, query, limit, module_hint))
    cols = r.fts_columns()
    body_cols = [c for c in cols if c.lower() not in ("name", "params")]
    if not body_cols and not _NAME_QUERY_RE.fullmatch(query):
        return _echo_config({
            "bodies_indexed": False,
            "searched_columns": cols,
            "query": query,
            "error": ("тела методов не проиндексированы: methods_fts "
                      f"содержит только {', '.join(cols)} (сверено с "
                      "ddl.rs vendored gyrfalcon) — буквальный поиск по "
                      "коду недоступен"),
            "hint": ("имена методов: search_function или именной запрос "
                     "code_grep («скидк», «скидк*»); по содержимому кода — "
                     "grep по COMPASS_SOURCE_ROOT; смысловое — codesearch "
                     "(после векторизации)"),
        })
    if body_cols:
        col_idx = cols.index(body_cols[0])
    else:
        # name-only схема: сниппет по колонке name (compass-cdf)
        col_idx = next((i for i, c in enumerate(cols)
                        if c.lower() == "name"), 0)
    sql = f"""SELECT m.id, m.name, m.type, m.is_export,
                     snippet(methods_fts, {int(col_idx)}, '[', ']', '…', 12)
                         AS snippet,
                     mo.rel_path, mo.object_name, mo.module_type,
                     rank AS fts_rank
              FROM methods_fts
              JOIN methods m ON m.id = methods_fts.rowid
              JOIN modules mo ON mo.id = m.module_id
              WHERE methods_fts MATCH ?"""
    params: list[Any] = [query]
    hint = _module_hint_for_search(module_hint)
    if hint:
        sql += " AND mo.rel_path LIKE ?"
        params.append(f"%{hint}%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(max(1, int(limit)) + 1)
    try:
        rows = r._rows(sql, tuple(params))
    except sqlite3.OperationalError as exc:
        return _echo_config({"error": f"FTS5 не принял запрос: {exc}",
                             "hint": "синтаксис FTS5: слова = И, «часть*» — "
                                     "префикс"})
    truncated = len(rows) > limit
    rows = rows[:limit]
    if truncated:
        rows.append({"_truncated": True, "limit": limit,
                     "hint": "показаны первые {0} по rank; уточни запрос "
                             "или module_hint".format(limit)})
    return _echo_config(rows)


def _code_grep_bodies(r: GyrReader, query: str, limit: int,
                      module_hint: str | None) -> dict:
    """Ветка тел (compass-gbi): FTS5 MATCH по methods_bodies_fts со
    сниппетом по колонке text. rowid FTS = method_id = methods.id."""
    if not query:
        return {"bodies_indexed": True, "results": [],
                "error": "пустой запрос",
                "hint": "слова (И), «часть*» — префикс, кавычки — фраза"}
    sql = """SELECT m.id, m.name, m.type, m.is_export,
                    snippet(methods_bodies_fts, 0, '[', ']', '…', 12)
                        AS snippet,
                    mo.rel_path, mo.object_name, mo.module_type,
                    rank AS fts_rank
             FROM methods_bodies_fts
             JOIN methods m ON m.id = methods_bodies_fts.rowid
             JOIN modules mo ON mo.id = m.module_id
             WHERE methods_bodies_fts MATCH ?"""
    params: list[Any] = [query]
    hint = _module_hint_for_search(module_hint)
    if hint:
        sql += " AND mo.rel_path LIKE ?"
        params.append(f"%{hint}%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(max(1, min(int(limit), 100)) + 1)
    try:
        rows = r._rows(sql, tuple(params))
    except sqlite3.OperationalError as exc:
        return {"bodies_indexed": True,
                "error": f"FTS5 не принял запрос: {exc}",
                "hint": "синтаксис FTS5: слова = И, «часть*» — префикс, "
                        "кавычки — фраза; подстрока внутри CamelCase-"
                        "идентификатора — source_grep"}
    truncated = len(rows) > limit
    rows = rows[:limit]
    out: dict[str, Any] = {"bodies_indexed": True, "results": rows,
                           "total": len(rows), "query": query}
    if truncated:
        out["_truncated"] = True
        out["hint"] = (f"показаны первые {limit} по rank; уточни запрос "
                       "или module_hint")
    return out


# обратная связь №4 (compass-d9t): дедлайн полного обхода source_root
_SOURCE_GREP_TIMEOUT = float(os.getenv("SOURCE_GREP_TIMEOUT", "30"))
# sj2: не более 2 одновременных full-scan — под пачкой конкурентных
# вызовов обходы не выедают CPU/IO целиком (на drvfs одиночный вызов
# из 9 параллельных подвисал > 120 с), дедлайн каждого скана честный.
# Семафор — на активный event loop (asyncio-примитивы привязываются к
# циклу при первом использовании: сервер FastMCP и тесты с asyncio.run
# живут в разных циклах)
_SOURCE_GREP_SEMAPHORES: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _grep_semaphore() -> asyncio.Semaphore:
    """Семафор параллели source_grep (макс. 2) — lazily на текущий loop."""
    loop = asyncio.get_running_loop()
    sem = _SOURCE_GREP_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(2)
        _SOURCE_GREP_SEMAPHORES[loop] = sem
    return sem


async def source_grep(pattern: str, module_hint: str | None = None,
                      limit: int = 20, context_lines: int = 2,
    ignorecase: bool = False) -> dict:
    """Regex/литеральный поиск по телам модулей активной конфигурации
    (compass-d9t, обратная связь №4): полный обход source_root с дедлайном;
    индекс не используется — честный truncated вместо имитации.

    pattern — Python regex (до 200 символов; для литерала экранируй точки);
    module_hint — подстрока относительного пути (сужает обход);
    limit — потолок матчей (1..100, дефолт 20); context_lines — строки
    контекста вокруг (0..5, дефолт 2); ignorecase — без учёта регистра.
    Ответ: {matches: [{file, line_no, line, context}],
    total_matches_returned, files_total, files_scanned, truncated,
    config: {id, short_name, version}}. Дедлайн — env SOURCE_GREP_TIMEOUT
    (секунды, дефолт 30): при превышении ответ честный truncated=true
    с числом просмотренных файлов.

    compass-sj2: обход блокирующий — выполняется в asyncio.to_thread,
    параллел ограничен семафором (не более 2 одновременных сканов):
    под пачкой конкурентных вызовов каждый обход получает свои CPU/IO,
    дедлайн остаётся достижим, событийный цикл не блокируется.
    """
    async with _grep_semaphore():
        # compass-yho: эхо конфигурации — единым хелпером поверх ВСЕХ веток
        # scan-а (error-ответы тоже привязаны к базе)
        return _echo_config(await asyncio.to_thread(
            _source_grep_scan, pattern, module_hint, limit, context_lines,
            ignorecase))


def _source_grep_scan(pattern: str, module_hint: str | None,
                      limit: int, context_lines: int,
                      ignorecase: bool) -> dict:
    """Синхронный full-scan source_root (поток to_thread, sj2)."""
    import re
    import time

    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "pattern обязателен"}
    if len(pattern) > 200:
        return {"error": "pattern длиннее 200 символов"}
    flags = re.IGNORECASE if ignorecase else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return {"error": f"некорректный regex: {exc}"}
    # источник — тот же, что у соседних тулов тела (read_function/
    # read_module): config.source_root (env COMPASS_SOURCE_ROOT → реестр)
    source_root = config.source_root
    if source_root is None or not Path(source_root).is_dir():
        return {"error": "source_root недоступен: не задан (COMPASS_SOURCE_ROOT "
                         "или активная конфигурация) либо каталог не найден"}
    source_root = Path(source_root)

    limit = max(1, min(int(limit), 100))
    context_lines = max(0, min(int(context_lines), 5))
    files = sorted(source_root.rglob("*.bsl"))
    if module_hint:
        hint = module_hint.lower()
        files = [f for f in files if hint in f.as_posix().lower()]
    deadline = time.monotonic() + _SOURCE_GREP_TIMEOUT
    started = time.monotonic()
    matches: list[dict] = []
    scanned = 0
    truncated = False
    for f in files:
        if time.monotonic() >= deadline:  # >=: нулевой таймаут честно режет
            truncated = True
            break
        try:
            lines = f.read_text(encoding="utf-8",
                                errors="replace").splitlines()
        except OSError:
            continue
        scanned += 1
        rel = f.relative_to(source_root).as_posix()
        last_hit = -10**9  # слияние соседних контекстов в одном файле
        for i, line in enumerate(lines):
            if rx.search(line):
                if i - last_hit <= context_lines + 1 and matches and \
                        matches[-1]["file"] == rel:
                    matches[-1]["line"] = line[:400]  # продолжение блока
                    last_hit = i
                    continue
                ctx = [f"{n + 1}: {lines[n][:400]}"
                       for n in range(max(0, i - context_lines),
                                      min(len(lines), i + context_lines + 1))]
                matches.append({"file": rel, "line_no": i + 1,
                                "line": line[:400], "context": ctx})
                last_hit = i
                if len(matches) >= limit:
                    truncated = True
                    break
        if len(matches) >= limit:
            break
    return {"matches": matches, "total_matches_returned": len(matches),
            "files_total": len(files), "files_scanned": scanned,
            "truncated": truncated,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def call_paths(method: str, direction: str = "down", depth: int = 10,
               module_hint: str | None = None,
               target: str | None = None) -> dict:
    """Дерево вызовов по таблице calls (Волна 1, compass-20c).

    direction="down" — кого вызывает метод (рёбра callee_name),
    "up" — кто его вызывает. Глубина — depth (дефолт 10), бюджет —
    CALL_PATHS_MAX_NODES узлов с честным _truncated; циклы защищены
    visited по id метода. target: вместо дерева — поиск пути до целевого
    метода (BFS — кратчайший по числу рёбер; имя цели матчится и по
    неразрешённому callee_name). Неоднозначное имя старта (несколько
    методов) — отказ со списком кандидатов и хинтом module_hint.
    Каждый ответ несёт config активной конфигурации (compass-yho),
    когда она выбрана.
    """
    r = reader()
    if r is None:
        return _echo_config(_no_index())
    direction = (direction or "").strip().lower()
    if direction not in ("down", "up"):
        return _echo_config(
            {"error": "direction: down (кого вызывает) | up (кто вызывает)"})
    try:
        depth = max(1, min(int(depth), 50))
    except (TypeError, ValueError):
        return _echo_config({"error": "depth — целое 1..50"})
    starts = r.get_method(method, _module_hint_for_search(module_hint))
    if not starts:
        return _echo_config({"error": f"метод '{method}' не найден",
                             "hint": "search_function по имени или module_hint"})
    if len(starts) > 1:
        return _echo_config(
            {"error": f"имя '{method}' неоднозначно: {len(starts)} методов",
             "candidates": [{"name": x["name"], "rel_path": x["rel_path"]}
                            for x in starts[:10]],
             "hint": "уточните module_hint («Документ.X», «Catalogs/X»…)"})
    start = starts[0]
    if target:
        return _echo_config(_call_path_to_target(r, start, direction, depth,
                                                 (target or "").strip()))
    root = _method_node(start)
    visited = {start["id"]}
    stats = {"nodes": 1, "truncated": False, "depth_reached": 0}
    frontier: list[tuple[dict, dict]] = [(start, root)]
    for level in range(depth):
        stats["depth_reached"] = level + 1
        next_frontier: list[tuple[dict, dict]] = []
        for row, node in frontier:
            children, expanded = _expand_node(r, row, direction, visited,
                                              stats)
            if children:
                node["children"] = children
            next_frontier.extend(expanded)
        if stats["truncated"] or not next_frontier:
            break
        frontier = next_frontier
    out: dict[str, Any] = {"method": method, "direction": direction,
                           "depth": depth, "root": root,
                           "nodes_total": stats["nodes"],
                           "depth_reached": stats["depth_reached"]}
    if stats["truncated"]:
        out["_truncated"] = True
        out["hint"] = (f"бюджет {CALL_PATHS_MAX_NODES} узлов исчерпан — "
                       "дерево обрезано; сужайте start/target или depth")
    return _echo_config(out)


def _method_node(row: dict) -> dict:
    """Узел дерева из строки methods×modules."""
    return {"name": row["name"], "rel_path": row.get("rel_path"),
            "object": row.get("object_name"),
            "module_type": row.get("module_type")}


def _expand_node(r: GyrReader, row: dict, direction: str,
                 visited: set, stats: dict
                 ) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Дети узла дерева: (children для JSON, пары (row, node) новых
    методов для следующего уровня). Бюджет узлов — честный _truncated;
    visited по id — защита от циклов и повторов между ветками."""
    children: list[dict] = []
    expanded: list[tuple[dict, dict]] = []
    if stats["nodes"] >= CALL_PATHS_MAX_NODES:
        stats["truncated"] = True
        return children, expanded
    if direction == "down":
        edges = r.callees(row["id"])
    else:
        edges = r.callers(row["name"])
    for e in edges:
        if stats["nodes"] >= CALL_PATHS_MAX_NODES:
            stats["truncated"] = True
            break
        if direction == "down":
            matches = r.resolve_callee(e["callee_name"], limit=1)
            child_row = matches[0] if matches else None
            name = e["callee_name"]
        else:
            child_row = r.method_by_id(e["caller_id"])
            name = e["caller_name"]
        if child_row is None:
            # вызов не разрешён в метод индекса — честный лист
            children.append({"name": name, "unresolved": True,
                             "step_line": e["line"],
                             "resolution": e["resolution"]})
        elif child_row["id"] in visited:
            children.append({**_method_node(child_row),
                             "cycle": True, "step_line": e["line"],
                             "resolution": e["resolution"]})
        else:
            visited.add(child_row["id"])
            # один и тот же объект и в дерево, и в next_frontier: следующий
            # уровень дописывает children именно в узел из дерева
            node = _method_node(child_row)
            node["step_line"] = e["line"]
            node["resolution"] = e["resolution"]
            children.append(node)
            expanded.append((child_row, node))
        stats["nodes"] += 1
    return children, expanded


def _edge_matches_target(name: str, target: str) -> bool:
    """Имя вызова/метода совпадает с целью: точно или «Модуль.Цель»."""
    if not name or not target:
        return False
    return (name.lower() == target.lower()
            or name.lower().endswith("." + target.lower()))


def _call_path_to_target(r: GyrReader, start: dict, direction: str,
                         depth: int, target: str) -> dict:
    """Поиск пути старт→target BFS'ом (кратчайший по рёбрам); цель может
    быть и неразрешённым именем вызова (callee_name без строки в methods)."""
    from collections import deque

    start_path = [{"name": start["name"], "rel_path": start["rel_path"],
                   "step_line": None, "resolution": None}]
    if _edge_matches_target(start["name"], target):
        return {"found": True, "target": target, "start": start["name"],
                "path": start_path, "note": "старт и есть цель"}
    queue: deque = deque([(start, start_path)])
    visited = {start["id"]}
    nodes = 1
    while queue and nodes < CALL_PATHS_MAX_NODES:
        row, path = queue.popleft()
        if len(path) - 1 >= depth:
            continue  # глубина исчерпана — дальше не расширяем
        if direction == "down":
            edges = [(e["callee_name"], None, e["line"], e["resolution"])
                     for e in r.callees(row["id"])]
        else:
            edges = [(e["caller_name"], e["caller_id"], e["line"],
                      e["resolution"]) for e in r.callers(row["name"])]
        for name, caller_id, line, resolution in edges:
            if _edge_matches_target(name, target):
                last_step = {"name": name, "rel_path": None,
                             "step_line": line, "resolution": resolution}
                note = ("последний шаг — имя вызова (в methods не разрешён)"
                        if direction == "down" else "путь найден")
                return {"found": True, "target": target,
                        "start": start["name"],
                        "path": path + [last_step], "note": note}
            if direction == "down":
                matches = r.resolve_callee(name, limit=1)
                child = matches[0] if matches else None
            else:
                child = r.method_by_id(caller_id)
            if child is None or child["id"] in visited:
                continue
            visited.add(child["id"])
            nodes += 1
            queue.append((child, path + [{"name": child["name"],
                                          "rel_path": child["rel_path"],
                                          "step_line": line,
                                          "resolution": resolution}]))
    return {"found": False, "target": target, "start": start["name"],
            "depth": depth,
            "hint": "пути нет в пределах depth/бюджета узлов — проверьте "
                    "имена (read_function) или увеличьте depth"}


def who_creates(object_name: str, limit: int = 50) -> dict:
    """Кто создаёт объект (Волна 1, compass-20c): платформенные
    СоздатьДокумент/СоздатьЭлемент/СоздатьНаборЗаписей/Создать в
    callee_name (рёбра calls) + create-строки metadata_code_usages +
    ссылки metadata_references. Имя канонизируется («Документ.X» → «X»)."""
    object_name = _canonical_object_name(object_name)
    r = reader()
    if r is None:
        return _no_index()
    creators = r.creators(object_name, limit=limit)
    usages = [u for u in r.code_usages(object_name, limit=limit)
              if (u.get("usage_kind") or "").lower() == "create"]
    if not creators and not usages:
        return {"_no_matches": True, "object": object_name,
                "hint": "созданий платформенными методами не найдено; "
                        "проверь имя (object_overview) или suffix — "
                        "Создать/СоздатьДокумент/СоздатьЭлемент/"
                        "СоздатьНаборЗаписей"}
    refs_total = r._rows(
        "SELECT COUNT(*) AS n FROM metadata_references WHERE ref_object LIKE ?",
        (f"%{object_name}%",))[0]["n"]
    return {"object": object_name,
            "creators": creators, "creators_total": len(creators),
            "create_usages": usages[:limit],
            "create_usages_total": len(usages),
            "referenced_by_total": refs_total,
            "references_sample": r.references_to(object_name, limit=5)}


def check_claims(claims: list[dict]) -> dict:
    """Батч-оракул (Волна 1, compass-20c): пачка утверждений одним
    вызовом. Каждый элемент — {kind: field, object, field} или
    {kind: call, caller, callee}; вердикты те же, что у verify_field/
    verify_call (holds + evidence). Кривой элемент — invalid_claim
    (holds: None), не ошибка всей пачки."""
    if not isinstance(claims, list) or not claims:
        return {"error": "claims — непустой список утверждений "
                         "{kind: field|call, ...}"}
    results: list[dict] = []
    counts = {"holds_true": 0, "holds_false": 0, "holds_unknown": 0}
    for i, claim in enumerate(claims):
        try:
            verdict = _check_single_claim(claim)
        except Exception as exc:  # один кривой элемент не роняет пачку
            verdict = {"holds": None, "verdict": "error",
                       "error": f"{type(exc).__name__}: {exc}"}
        key = ("holds_true" if verdict.get("holds") is True
               else "holds_false" if verdict.get("holds") is False
               else "holds_unknown")
        counts[key] += 1
        # config-бейдж в каждый элемент (compass-4w7.3): field-вердикты
        # несут его от verify_field (перезапись идемпотентна), call-вердикты
        # получают здесь — verify_call по обратке не трогается
        results.append({"index": i, "claim": claim, **_config_badge(),
                        **verdict})
    return {"results": results, "total": len(claims), **counts}


def _check_single_claim(claim: Any) -> dict:
    """Один элемент батча: валидация → verify_field/verify_call (та же
    SQL-логика, переиспользование по ТЗ)."""
    if not isinstance(claim, dict):
        return {"holds": None, "verdict": "invalid_claim",
                "error": "элемент claims не словарь"}
    kind = (claim.get("kind") or "").strip().lower()
    if kind == "field":
        obj = (claim.get("object") or "").strip()
        fld = (claim.get("field") or "").strip()
        if not obj or not fld:
            return {"holds": None, "verdict": "invalid_claim",
                    "error": "kind=field требует object и field"}
        return verify_field(obj, fld)
    if kind == "call":
        caller = (claim.get("caller") or "").strip()
        callee = (claim.get("callee") or "").strip()
        if not caller or not callee:
            return {"holds": None, "verdict": "invalid_claim",
                    "error": "kind=call требует caller и callee"}
        return verify_call(caller, callee)
    return {"holds": None, "verdict": "invalid_claim",
            "error": f"kind '{claim.get('kind')}' — только field|call"}


def index_freshness(cid: str = "") -> dict:
    """Свежесть индекса (Волна 1, compass-20c): max(modules.mtime)
    против indexed_at записи реестра → «исходник новее индекса на N»
    или «свежо». ВАЖНО (сверено с ddl.rs v0.1.20): строитель gyrfalcon
    modules.mtime НЕ заполняет — на живых индексах вердикт честно
    «не определим», с built_at/indexed_at на руках; сравнение заработает,
    когда строитель начнёт писать mtime (или после инкремента, который
    пишет только size)."""
    from datetime import datetime

    from ..context import active_config_id
    from ..registry import get_config

    r = reader()
    if r is None:
        return _no_index()
    target = (cid or "").strip() or active_config_id()
    entry = get_config(target) if target else None
    snap = r.freshness_snapshot()
    out: dict[str, Any] = {"indexed_at": (entry or {}).get("indexed_at"),
                           "built_at": snap["built_at"],
                           "modules_total": snap["modules_total"]}
    max_mtime = snap["max_module_mtime"]
    indexed_at = out["indexed_at"]
    if max_mtime is None:
        out.update({"status": "unknown",
                    "verdict": "свежесть не определима: modules.mtime "
                               "не заполняется строителем (сверено с "
                               "ddl.rs vendored gyrfalcon)",
                    "hint": "возраст индекса — по built_at/indexed_at; "
                            "догнать правки .bsl — rebuild_incremental"})
        return out
    if not indexed_at:
        out.update({"status": "unknown",
                    "verdict": "в реестре нет indexed_at — сборка "
                               "не зафиксирована"})
        return out
    try:
        indexed_epoch = datetime.fromisoformat(indexed_at).timestamp()
    except ValueError:
        out.update({"status": "unknown",
                    "verdict": f"indexed_at не разбирается: {indexed_at}"})
        return out
    if max_mtime > indexed_epoch:
        lag = int(max_mtime - indexed_epoch)
        out.update({"status": "stale", "lag_seconds": lag,
                    "verdict": "исходник новее индекса на "
                               + _fmt_lag(lag),
                    "hint": "догнать правки .bsl — rebuild_incremental"})
    else:
        out.update({"status": "fresh", "verdict": "свежо"})
    return out


def _fmt_lag(seconds: int) -> str:
    """Лаг в человекочитаемом виде: часы/минуты/секунды."""
    if seconds >= 3600:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    if seconds >= 60:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


# ------------------------------------------------------- инкрементальная сборка

def rebuild_incremental(cid: str = "") -> dict:
    """MCP-тул: инкрементальная пересборка индекса (compass-941.4/Т3-Ф5).

    Дифф выгрузки против таблицы modules индекса (size/mtime — сверено
    с ddl.rs v0.1.20), переиндексация только изменённых/удалённых .bsl
    через gyrfalcon update --db <индекс> <файлы…>; механизм сборки общий
    с полной (BUILD_STATE + watchdog fc9). cid пуст — активная
    конфигурация контекста. Новые файлы дифф не находит (их нет в
    modules) — честная пометка в ответе; правки XML принимает только
    полная сборка. Подробности и refusal-ветки — registry.incremental_rebuild.
    """
    from ..registry import incremental_rebuild
    from ..context import active_config_id

    target = (cid or "").strip() or active_config_id()
    if not target:
        return _no_index("no_config")
    return incremental_rebuild(target)


# ------------------------------------------------------------------- статус

def index_status() -> dict:
    """Состояние индекса активной конфигурации контекста.

    Счётчики — из кэша реестра (пишется при сборке): живой stats() через
    drvfs занимает минуты и вешал панель (инцидент 2026-11-05).
    """
    if not config.index_ready:
        return {"status": "not_configured",
                "hint": "конфигурация не выбрана или индекс не собран; "
                        "config_list → config_use, либо сборка через панель",
                "runtime": _runtime_brief()}
    try:
        from ..registry import get_config
        entry = get_config(config.active_config_id) or {}
        cached = entry.get("index_counts") or {}
        if cached:
            return {"status": "ok", "counts": cached,
                    "runtime": _runtime_brief()}
    except Exception:
        pass
    r = reader()
    if r is None:
        return {"status": "error"}
    return {"status": "ok", "counts": r.stats(), "runtime": _runtime_brief()}
