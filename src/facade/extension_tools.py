# -*- coding: utf-8 -*-
"""Тулы механизма расширений (Т3): стек, перехваты, эффективный поток.

Семантика — arch-t3-extensions-draft.md v0.5 §5 (верифицирована по книге
Хрусталевой «Расширения конфигураций»): слои; &Перед — сверху вниз (по
УБЫВАНИЮ effective_position), &После — снизу вверх (ВОЗРАСТАНИЕ),
активный &Вместо — верхний слой (max effective_position), прочие затенены
(цепочка ПродолжитьВызов()). Эффективный порядок стека — v0.5 §4: сортировка
(назначение, позиция в списке конфигуратора); order_confirmed=True только
для порядка из рантайма.

Директива владельца (v0.4): position и purpose расширения ОБЯЗАТЕЛЬНЫ —
платформа их в файлах не хранит; вызов без них — подсказка агенту, не
угадывание.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import tools

# P1-3 (аудит 4l5): имя расширения становится путём extensions/<cid>/<Имя>/
# — только буквы (латиница/кириллица), цифры и подчёркивание, без
# разделителей путей и '..', потолок 80 символов (лимит имени 1С).
_EXT_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_]{1,80}$")

# Подсказка директивы v0.4 (единый текст для тулов и панели)
ADD_DIRECTIVE_HINT = (
    "Подсказка: добавление расширения требует двух фактов, которых нет в "
    "файлах выгрузки — уточните у разработчика: (1) позиция расширения в "
    "списке «Расширения конфигуратора» (1 = верх списка), (2) назначение "
    "(Исправление/Patch, Адаптация/Customization, Дополнение/Addition). "
    "Они задают порядок применения и тени; без них регистрация не выполняется."
)

EMPTY_STACK_HINT = ("стек расширений пуст — порядок не подтверждён; "
                    "добавьте расширения через extension_add (позиция и "
                    "назначение — у разработчика)")


def ext_hub_root() -> Path:
    """Корень хаба выгрузок расширений: env EXT_HUB, если путь существует
    ЗДЕСЬ (host-run), иначе маунт-пойнт /ext-dumps (compass-85s; bcl:
    compose пробрасывает host-значения для панели — в контейнере они
    не существуют). Список выгрузок для панели — /api/dumps/available
    (b1), здесь — только источник для extension_add(source="hub")."""
    val = os.getenv("EXT_HUB")
    if val and Path(val).is_dir():
        return Path(val)
    return Path("/ext-dumps")


def _source_path_error(source: str) -> str | None:
    """Честный отказ для source-пути расширения (P1-3, аудит 4l5), иначе None.

    Допустимы: "hub", "" (в caller) и путь — только АБСОЛЮТНЫЙ, без
    сегментов '..', при этом либо внутри объявленного хаба EXT_HUB, либо
    существующий каталог выгрузки (копия из него явно упомянута в ответе
    полем copied_from). Относительные пути и обход каталогов — отказ."""
    p = Path(source)
    if not p.is_absolute():
        return (f"source '{source}' — относительный путь не допускается: "
                "укажите абсолютный путь выгрузки, \"hub\" или пустую строку")
    if ".." in p.parts:
        return (f"source '{source}' содержит сегмент '..' — обход "
                "каталогов не допускается")
    try:
        p.resolve().relative_to(ext_hub_root().resolve())
        return None                      # внутри объявленного хаба EXT_HUB
    except ValueError:
        pass
    if p.is_dir():
        return None                      # существующий абсолютный каталог
    return (f"source '{source}' вне хаба EXT_HUB и не существует — допустимы: "
            f"\"hub\", абсолютный путь внутри EXT_HUB ({ext_hub_root()}) или "
            "существующий каталог выгрузки")


def _copy_extension_home(cid: str, name: str, source: str) -> dict:
    """Копия выгрузки расширения в дом: extensions/<cid>/<Имя>/
    (config_extension_root) — дальше действующая схема (симлинк ext/
    ставит ensure_ext_link при сборке/инкременте, пересборка подхватит).

    source="hub" — выгрузка из хаба EXT_HUB (/ext-dumps/<Имя>/); иначе
    source — путь каталога выгрузки (P1-3: абсолютный, без '..', внутри
    EXT_HUB или существующий каталог — остальное честный отказ).
    Честные отказы: источник не найден, дом расширения уже занят (молча
    перезаписывать исходники нельзя). В выгрузке нет Configuration.xml и
    *.cfe → копия выполняется, но с предупреждением (не похоже на
    выгрузку расширения)."""
    import shutil

    from ..registry import config_extension_root
    if source == "hub":
        src = ext_hub_root() / name
    else:
        err = _source_path_error(source)
        if err:
            return {"error": err}
        src = Path(source)
    if not src.is_dir():
        hint = (f"хаб EXT_HUB: {ext_hub_root()} (список выгрузок — "
                "/api/dumps/available)" if source == "hub" else
                "проверьте путь source")
        return {"error": f"выгрузка расширения не найдена: {src} — {hint}"}
    has_marker = ((src / "Configuration.xml").exists()
                  or any(src.glob("*.cfe")))
    dst = config_extension_root(cid, name)
    if dst.exists() and any(dst.iterdir()):
        return {"error": f"дом расширения занят: {dst} — обновление "
                         "исходников сделайте вручную и пересборкой; "
                         "если исходники уже в доме — повторите без "
                         "source (регистрация без копирования)"}
    try:
        shutil.copytree(src, dst)
    except OSError as exc:
        return {"error": f"копия {src} → {dst} не удалась: "
                         f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"copied_from": str(src), "extension_home": str(dst)}
    if not has_marker:
        out["warning"] = ("в выгрузке нет Configuration.xml и *.cfe — "
                          "не похоже на выгрузку расширения; сборка "
                          "подскажет точнее")
    return out


def _stack_context() -> tuple[list[dict], dict[str, dict], bool]:
    """Стек активной конфигурации: (список в эффективном порядке,
    словарь имя→запись, order_confirmed)."""
    from ..context import active_config_id
    from .. import registry as registry_mod
    cid = active_config_id()
    stack = registry_mod.stack_of(cid) if cid else []
    by_name = {e["name"]: e for e in stack}
    confirmed = bool(stack) and all(e.get("order_confirmed") for e in stack)
    return stack, by_name, confirmed


def _kind_of(annotation: str | None) -> str:
    """Аннотация → kind, толерантно (формат живого индекса проверит
    владелец на Ф1 — работаем по подстроке, не по строгому равенству)."""
    a = annotation or ""
    if "Вместо" in a:
        return "instead"
    if "ИзменениеИКонтроль" in a:
        return "modify_control"
    if "Перед" in a:
        return "before"
    if "После" in a:
        return "after"
    return "other"


def _split_dev(rows: list[dict], by_name: dict[str, dict],
               include_dev: bool) -> tuple[list[dict], int]:
    """dev-расширения: по умолчанию исключаются из выдачи (arch §8) со
    счётчиком; include_dev=True — показываются с пометкой dev."""
    kept: list[dict] = []
    hidden = 0
    for row in rows:
        entry = by_name.get(row.get("extension_name") or "")
        if entry and entry.get("status") == "dev" and not include_dev:
            hidden += 1
            continue
        row = {**row}
        if entry and entry.get("status") == "dev":
            row["dev"] = True
        kept.append(row)
    return kept, hidden


def _notes_for(by_name: dict[str, dict], befores: int,
               insteads: list[dict]) -> list[str]:
    """Честные пометки платформенной семантики (arch §5/§9)."""
    notes: list[str] = []
    if befores:
        notes.append("цепочка &Перед обрывается при Отказ в вышележащем "
                     "&Перед — нижележащие не запускаются")
    if any(b["kind"] == "modify_control" for b in insteads):
        notes.append("&ИзменениеИКонтроль: тело = результат модификации "
                     "(#Вставка/#Удаление); ограничение по назначению "
                     "«Исправление» не сверено с докой (36.4.2.2.5)")
    if any(e.get("safe_mode") for e in by_name.values()):
        notes.append("в стеке есть расширение в безопасном режиме — "
                     "аннотированные серверные перехваты не выполняются")
    return notes


def extension_stack() -> dict | list:
    """Стек активной конфигурации: эффективный порядок (реестр) + счётчики
    из индекса + СВЕРКА имён: расширение есть в индексе, но не в стеке →
    предупреждение «добавьте через extension_add (нужны позиция и
    назначение)»; есть в стеке, нет в индексе → предупреждение."""
    r = tools.reader()
    if r is None:
        return tools._no_index()
    stack, by_name, confirmed = _stack_context()
    index_exts = r.extension_counts()
    warnings: list[dict] = []
    index_names = {e["name"] for e in index_exts}
    for n in sorted(index_names - set(by_name)):
        warnings.append({
            "extension": n, "kind": "index_only",
            "text": f"«{n}» найдено в индексе, но нет в стеке — добавьте "
                    "через extension_add (нужны позиция и назначение)"})
    for n in sorted(set(by_name) - index_names):
        warnings.append({
            "extension": n, "kind": "stack_only",
            "text": f"«{n}» есть в стеке, но не найден в индексе — проверьте "
                    "ext/-соседа выгрузки и пересоберите индекс"})
    return {"stack": stack, "extensions": index_exts,
            "order_confirmed": confirmed, "warnings": warnings}


def extension_add(name: str, position: int = 0, purpose: str = "",
                  status: str = "stable", source: str = "") -> dict:
    """Добавить расширение в стек АКТИВНОЙ конфигурации.

    ⚠️ Директива владельца (v0.4): position (1 = верх списка конфигуратора)
    и purpose (Исправление|Адаптация|Дополнение /
    Patch|Customization|Addition) ОБЯЗАТЕЛЬНЫ — спросите разработчика,
    если не знаете: они задают порядок применения. Вызов без них → не
    ошибка, а подсказка с этим текстом.

    source (compass-85s): "" — исходники уже на месте (прежний сценарий,
    копирования нет); "hub" — скопировать выгрузку из хаба EXT_HUB
    (/ext-dumps/<Имя>/, read-only маунт); путь — скопировать из указанного
    каталога (P1-3, аудит 4l5: абсолютный, без '..', внутри EXT_HUB или
    существующий каталог; источник явно упомянут в ответе — copied_from).
    Копия идёт в дом extensions/<cid>/<Имя>/; симлинк ext/ и пересборка
    индекса — действующая схема (ensure_ext_link). Отказ копии отменяет
    регистрацию.

    P1-3: имя расширения валидируется (_EXT_NAME_RE) — оно становится
    путём extensions/<cid>/<Имя>/; невалидное (разделители путей, '..',
    пробелы, длиннее 80) — честный отказ.
    """
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = 0
    if not (name or "").strip() or position < 1 or not (purpose or "").strip():
        return {"hint": ADD_DIRECTIVE_HINT, "name": name,
                "accepted_purpose_values":
                    ["Исправление/Patch", "Адаптация/Customization",
                     "Дополнение/Addition"]}
    name = name.strip()
    if not _EXT_NAME_RE.fullmatch(name):
        return {"error": f"имя расширения '{name}' недопустимо — только буквы "
                         "(латиница/кириллица), цифры и подчёркивание, до 80 "
                         "символов; разделители путей и '..' не допускаются "
                         "(имя становится каталогом extensions/<cid>/<Имя>)"}
    from ..context import active_config_id
    from .. import registry as registry_mod
    cid = active_config_id()
    if not cid:
        return {"error": "активная конфигурация не выбрана — config_use",
                "hint": ADD_DIRECTIVE_HINT}
    copied = None
    if (source or "").strip():
        # копия до регистрации: отказ копии отменяет добавление
        copied = _copy_extension_home(cid, name, source.strip())
        if "error" in copied:
            return copied
    out = registry_mod.stack_add(cid, name, position, purpose, status)
    if copied:
        out.update(copied)   # copied_from/extension_home (+warning)
    return out


def extension_remove(name: str) -> dict:
    """Убрать расширение из стека активной конфигурации (запись реестра;
    каталог ext/ и индекс не трогаются)."""
    from ..context import active_config_id
    from .. import registry as registry_mod
    cid = active_config_id()
    if not cid:
        return {"error": "активная конфигурация не выбрана — config_use"}
    return registry_mod.stack_remove(cid, name)


def extension_move(name: str, position: int) -> dict:
    """Сменить позицию расширения в списке конфигуратора (эффективный
    порядок пересчитывается внутри назначения)."""
    from ..context import active_config_id
    from .. import registry as registry_mod
    cid = active_config_id()
    if not cid:
        return {"error": "активная конфигурация не выбрана — config_use"}
    return registry_mod.stack_move(cid, name, position)


def who_overrides(method: str, module_hint: str | None = None,
                  include_dev: bool = False) -> dict | list:
    """Все перехваты метода по стеку: аннотация (kind), расширение,
    effective_position, active/shadowed (для &Вместо — активен верхний по
    effective_position), lines перехватчика. dev — скрыты с пометкой
    dev_hidden=N (include_dev=True — показать с флагом dev)."""
    r = tools.reader()
    if r is None:
        return tools._no_index()
    stack, by_name, confirmed = _stack_context()
    rows = r.overrides_of_method(method)
    if module_hint:
        # F4/kwn: русская подсказка «Справочник.X» → «Catalogs/X» — иначе
        # подстрока не матчится с путём перехватчика
        hint = tools._module_hint_for_search(module_hint).lower()
        rows = [x for x in rows
                if (x.get("intercept_path") or "")
                and hint in x["intercept_path"].lower()]
    if not rows:
        return {"_no_matches": True, "method": method,
                "hint": "перехватов метода нет в extension_overrides — "
                        "цель не расширяется"}
    rows, dev_hidden = _split_dev(rows, by_name, include_dev)
    if not rows:
        return {"_no_matches": True, "method": method,
                "dev_hidden": dev_hidden,
                "hint": "все перехваты в dev-расширениях (include_dev=True — "
                        "показать)"}
    insteads = [x for x in rows if _kind_of(x.get("annotation")) == "instead"]
    active_pos = None
    if insteads and all(
            by_name.get(x.get("extension_name") or "")
            and by_name[x["extension_name"]].get("effective_position") is not None
            for x in insteads):
        active_pos = max(by_name[x["extension_name"]]["effective_position"]
                         for x in insteads)
    for row in rows:
        entry = by_name.get(row.get("extension_name") or "")
        row["kind"] = _kind_of(row.get("annotation"))
        if entry:
            row["effective_position"] = entry["effective_position"]
            row["purpose"] = entry["purpose"]
        if row["kind"] == "instead":
            row["active"] = (row["effective_position"] == active_pos
                             if active_pos is not None else None)
            if row["active"] is not None:
                row["shadowed"] = not row["active"]
    out: dict[str, Any] = {"method": method, "overrides": rows,
                           "dev_hidden": dev_hidden,
                           "order_confirmed": confirmed}
    if not stack:
        out["hint"] = EMPTY_STACK_HINT
    return out


def effective_method(method: str, module_hint: str | None = None,
                     include_dev: bool = False) -> dict | list:
    """Эффективный поток исполнения метода с учётом стека (arch §5):
    flow: [&Перед … по УБЫВАНИЮ effective_position] → body (база; если у
    цели &ИзменениеИКонтроль — пометка «модуль из расширения») →
    [&После … по ВОЗРАСТАНИЮ]; &Вместо: активный (max effective_position)
    замещает body, прочие — shadowed-цепочка (ПродолжитьВызов).
    Атрибуция каждого блока: source, effective_position, rel_path, lines.
    order_confirmed стека, dev_hidden: N. Без стека — честный перечень
    перехватов без порядка (order_confirmed: false)."""
    r = tools.reader()
    if r is None:
        return tools._no_index()
    stack, by_name, confirmed = _stack_context()
    rows = r.overrides_of_method(method)
    if module_hint:
        # F4/kwn: русская подсказка «Справочник.X» → «Catalogs/X» — иначе
        # подстрока не матчится с путём перехватчика
        hint = tools._module_hint_for_search(module_hint).lower()
        rows = [x for x in rows
                if (x.get("intercept_path") or "")
                and hint in x["intercept_path"].lower()]
    if not rows:
        return {"_no_matches": True, "method": method,
                "hint": "перехватов нет — метод не расширяется; тело и "
                        "вызовы: read_function"}
    if not stack:
        listing = [{"extension": x.get("extension_name"),
                    "kind": _kind_of(x.get("annotation")),
                    "annotation": x.get("annotation"),
                    "rel_path": x.get("intercept_path"),
                    "lines": [x.get("intercept_line"),
                              x.get("intercept_end_line")]}
                   for x in rows]
        return {"method": method, "order_confirmed": False,
                "overrides": listing, "hint": EMPTY_STACK_HINT}
    rows, dev_hidden = _split_dev(rows, by_name, include_dev)

    def _block(row: dict, kind: str) -> dict:
        entry = by_name.get(row.get("extension_name") or "")
        block: dict[str, Any] = {
            "kind": kind, "source": row.get("extension_name"),
            # вне стека (index_only) позиция неизвестна — не верхний слой
            "effective_position":
                entry["effective_position"] if entry else None,
            "rel_path": row.get("intercept_path"),
            "lines": [row.get("intercept_line"),
                      row.get("intercept_end_line")]}
        if row.get("extension_method"):
            block["ext_method"] = row["extension_method"]
        if row.get("dev"):
            block["dev"] = True
        return block

    befores = [_block(x, "before") for x in rows
               if _kind_of(x.get("annotation")) == "before"]
    afters = [_block(x, "after") for x in rows
              if _kind_of(x.get("annotation")) == "after"]
    modify = [_block(x, "modify_control") for x in rows
              if _kind_of(x.get("annotation")) == "modify_control"]
    instead_rows = [x for x in rows
                    if _kind_of(x.get("annotation")) == "instead"]
    other = [_block(x, "other") for x in rows
             if _kind_of(x.get("annotation")) not in
             ("before", "after", "instead", "modify_control")]

    flow: list[dict] = sorted(befores,
                              key=lambda b: -(b["effective_position"] or 0))
    body_block: dict[str, Any] = {"kind": "body", "source": "base",
                                  "effective_position": 0}
    base = next((x for x in r.get_method(method)
                 if not (x["rel_path"] or "").startswith("../")), None)
    if base is not None:
        body_block.update({"rel_path": base["rel_path"],
                           "lines": [base["line"], base["end_line"]]})
    else:
        body_block["note"] = ("модуль цели не найден в основной конфигурации "
                              "(перехват цели из расширения / "
                              "&ИзменениеИКонтроль?)")
    flow.append(body_block)
    flow.extend(sorted(modify, key=lambda b: -(b["effective_position"] or 0)))
    insteads = sorted((_block(x, "instead") for x in instead_rows),
                      key=lambda b: -(b["effective_position"] or 0))
    if insteads:
        active = insteads[0]
        active["shadowed"] = [
            {k: b[k] for k in ("source", "effective_position", "rel_path",
                               "lines", "ext_method") if k in b}
            for b in insteads[1:]]
        flow.append(active)
    flow.extend(sorted(afters, key=lambda b: (b["effective_position"] or 0)))
    flow.extend(other)
    out = {"method": method, "flow": flow, "order_confirmed": confirmed,
           "dev_hidden": dev_hidden}
    notes = _notes_for(by_name, len(befores), insteads)
    if notes:
        out["notes"] = notes
    return out


def extension_sync_from_ping(ping_result: dict | None = None,
                             cid: str = "") -> dict:
    """Пинг-синк стека расширений с живой базой (Ф4, arch §3-L2①).

    Сверяет СОСТАВ и версии расширений стека реестра с Пингом 0.2.0
    ({Имя, Версия, Активно}). Без ping_result — Пинг у рантайма
    (нужен RUNTIME_URL; офлайн-режим — честная подсказка про
    offline_submit). Порядок применения из API платформы недоступен
    (Пинг: ПорядокДоступен=Ложь) — позиции и order_source не меняются;
    расширения из базы без регистрации в стеке НЕ добавляются сами
    (директива v0.4: позиция и назначение — у разработчика,
    extension_add).

    ping_result — готовый ответ Пинга (панель/офлайн-конвейер), тогда
    сеть не трогается; cid — конфигурация синка (по умолчанию активная
    контекста; панель передаёт выбранную).
    """
    ping = ping_result
    if not isinstance(ping, dict):
        from ..runtime.client import RuntimeUnavailable, runtime_client
        from .runtime_tools import _offline_mode
        if _offline_mode():
            return {"hint": "офлайн-режим: HTTP-Пинг недоступен — соберите "
                            "пакет offline_submit([{\"method\": \"ping\"}]), "
                            "заберите ответ offline_wait и передайте его "
                            "сюда параметром ping_result"}
        try:
            ping = runtime_client().ping()
        except RuntimeUnavailable as exc:
            return {"error": f"Пинг недоступен: {exc}"}
    extensions = ping.get("Расширения")
    if not isinstance(extensions, list):
        err = ping.get("РасширенияОшибка") or "Пинг не вернул список расширений"
        return {"error": f"Пинг без расширений: {err}"}
    from .. import registry as registry_mod
    from ..context import active_config_id
    target = (cid or "").strip() or active_config_id()
    if not target:
        return {"error": "активная конфигурация не выбрана"}
    report = registry_mod.sync_stack_from_ping(target, extensions)
    if "error" in report:
        return report
    report["order_available"] = bool(ping.get("ПорядокДоступен"))
    return report
