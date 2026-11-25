# -*- coding: utf-8 -*-
"""Тела методов в индексе: methods_bodies + methods_bodies_fts (compass-gbi).

Пост-шаг НАШЕЙ сборки (vendored gyrfalcon не трогается): после gyrfalcon
build/update в index.db создаются таблицы

    methods_bodies(method_id PK, text)               — тексты методов
    methods_bodies_fts (FTS5 external-content,        — полнотекстовый слой
        content='methods_bodies', content_rowid='method_id', unicode61)

Синхронизация FTS — триггеры (insert/update/delete на methods_bodies),
поэтому перезаливка строк сама обновляет индекс. Текст метода — строки
line_start..line_end из methods, файл читается кэшем источников GyrReader
(compass-1mj.8). Токенизация unicode61 — по словам: CamelCase-идентификатор
цельный токен, поиск подстроки внутри него недоступен (для этого
source_grep); строка объявления «Процедура Имя(...)» входит в тело, имена
ищутся тем же запросом.

env COMPASS_BODIES_FTS=off отключает и заливку, и поиск (объём диска).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

# размер пачки INSERT и страницы обхода methods
_BATCH = 2000
_PAGE = 5000
# потолок переменных в IN (...): старый лимит sqlite 999 — с запасом
_IN_CHUNK = 500

_DDL = """
CREATE TABLE IF NOT EXISTS methods_bodies (
    method_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS methods_bodies_fts USING fts5(
    text, content='methods_bodies', content_rowid='method_id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS methods_bodies_ai AFTER INSERT ON methods_bodies
BEGIN
    INSERT INTO methods_bodies_fts(rowid, text)
        VALUES (new.method_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS methods_bodies_ad AFTER DELETE ON methods_bodies
BEGIN
    INSERT INTO methods_bodies_fts(methods_bodies_fts, rowid, text)
        VALUES ('delete', old.method_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS methods_bodies_au AFTER UPDATE ON methods_bodies
BEGIN
    INSERT INTO methods_bodies_fts(methods_bodies_fts, rowid, text)
        VALUES ('delete', old.method_id, old.text);
    INSERT INTO methods_bodies_fts(rowid, text)
        VALUES (new.method_id, new.text);
END;
"""

_INSERT = "INSERT INTO methods_bodies VALUES (?, ?)"


def enabled() -> bool:
    """FTS тел включён? env COMPASS_BODIES_FTS=off — выключено."""
    return os.getenv("COMPASS_BODIES_FTS", "on").strip().lower() != "off"


def bodies_in_index(reader) -> bool:
    """В индексе есть залитые тела (строка в methods_bodies)."""
    if not enabled():
        return False
    try:
        return reader._rows("SELECT 1 FROM methods_bodies LIMIT 1") != []
    except sqlite3.OperationalError:
        return False  # таблиц нет — индекс без пост-шага (старая сборка)


def _connect(index_db: str | Path) -> sqlite3.Connection:
    """RW-соединение писателя пост-шага: таблицы/триггеры — при создании."""
    conn = sqlite3.connect(str(index_db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def _extract(reader, row: sqlite3.Row, source_root: str | Path) -> str | None:
    """Текст метода: строки line..end_line из файла выгрузки (кэш 1mj.8).

    None — метод без границ или файл короче заявленного диапазона
    (рассинхрон выгрузки и индекса): такое тело не заливаем.
    """
    line, end = row["line"], row["end_line"]
    if not line or not end or line < 1 or end < line:
        return None
    lines = reader.source_lines(row["rel_path"], source_root)
    if not lines or len(lines) < end:
        return None
    return "\n".join(lines[line - 1:end])


def fill_bodies(index_db: str | Path, source_root: str | Path) -> dict[str, Any]:
    """Полная заливка тел (пост-шаг полной сборки, под BUILD_STATE).

    Перезаливка идемпотентна: строки прошлого билда недействительны
    (ids методов пересозданы билдером) — DELETE перед INSERT, триггеры
    синхронизируют FTS. Коммит страницами: срыв на середине оставляет
    частично залитый, но консистентный слой (тело+FTS в одной транзакции
    страницы).
    """
    if not enabled():
        return {"status": "disabled", "env": "COMPASS_BODIES_FTS=off"}
    from .gyr_reader import GyrReader  # кэш источников (compass-1mj.8)

    t0 = time.monotonic()
    index_db = Path(index_db)
    reader = GyrReader(index_db)
    conn = _connect(index_db)
    filled = skipped = 0
    last_id = 0
    try:
        conn.execute("DELETE FROM methods_bodies")
        while True:
            page = conn.execute(
                """SELECT m.id, m.name, mo.rel_path, m.line, m.end_line
                   FROM methods m
                   JOIN modules mo ON mo.id = m.module_id
                   WHERE m.id > ? ORDER BY m.id LIMIT ?""",
                (last_id, _PAGE)).fetchall()
            if not page:
                break
            last_id = page[-1]["id"]
            rows = []
            for row in page:
                text = _extract(reader, row, source_root)
                if text is None:
                    skipped += 1
                    continue
                rows.append((row["id"], text))
            if rows:
                conn.executemany(_INSERT, rows)
                filled += len(rows)
            conn.commit()
    finally:
        conn.close()
        reader.close()
    return {"status": "filled", "bodies": filled, "skipped": skipped,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "db_bytes": index_db.stat().st_size}


def update_bodies(index_db: str | Path, source_root: str | Path,
                  changed: list[str] | None = None) -> dict[str, Any]:
    """Инкрементальная заливка (после gyrfalcon update, compass-gbi).

    changed — rel_path изменённых модулей (дифф из registry.incremental_
    rebuild); None — дифф по changed_modules. Перезаливаются тела методов
    этих модулей; осиротевшие строки (метод удалённых модулей билдер уже
    убрал из methods) вычищаются. Таблиц ещё нет — первый запуск
    разворачивается в fill_bodies. Новые файлы (не в modules) дифф не
    видит — их тела появятся при полной сборке.
    """
    if not enabled():
        return {"status": "disabled", "env": "COMPASS_BODIES_FTS=off"}
    from .gyr_reader import GyrReader

    t0 = time.monotonic()
    index_db = Path(index_db)
    reader = GyrReader(index_db)
    conn = sqlite3.connect(str(index_db), timeout=30)
    conn.row_factory = sqlite3.Row
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'methods_bodies'"
    ).fetchone() is not None
    conn.close()
    if not existed:
        return fill_bodies(index_db, source_root)
    conn = _connect(index_db)
    filled = pruned = 0
    try:
        changed = [c for c in (changed or []) if c]
        if not changed:
            diff = reader.changed_modules(source_root)
            changed = diff["changed"]
        # тела методов изменённых модулей — под перезаливку
        for i in range(0, len(changed), _IN_CHUNK):
            chunk = changed[i:i + _IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            conn.execute(
                f"""DELETE FROM methods_bodies WHERE method_id IN
                    (SELECT id FROM methods WHERE module_id IN
                     (SELECT id FROM modules WHERE rel_path IN ({ph})))""",
                chunk)
            conn.commit()
        # осиротевшие тела: методы удалённых модулей билдер убрал из methods
        pruned = conn.execute(
            "DELETE FROM methods_bodies WHERE method_id NOT IN "
            "(SELECT id FROM methods)").rowcount
        conn.commit()
        # перезаливка тел методов изменённых модулей
        for i in range(0, len(changed), _IN_CHUNK):
            chunk = changed[i:i + _IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = []
            for row in conn.execute(
                    f"""SELECT m.id, m.name, mo.rel_path, m.line, m.end_line
                        FROM methods m
                        JOIN modules mo ON mo.id = m.module_id
                        WHERE mo.rel_path IN ({ph})""", chunk):
                text = _extract(reader, row, source_root)
                if text is None:
                    continue
                rows.append((row["id"], text))
            if rows:
                conn.executemany(_INSERT, rows)
                filled += len(rows)
            conn.commit()
    finally:
        conn.close()
        reader.close()
    return {"status": "updated", "modules": len(changed),
            "bodies": filled, "pruned": pruned,
            "elapsed_s": round(time.monotonic() - t0, 1)}
