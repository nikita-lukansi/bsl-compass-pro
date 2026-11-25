# -*- coding: utf-8 -*-
"""Читатель индекса gyrfalcon (блок разведки): read-only поверх index.db.

Контракт — схема vendored gyrfalcon v0.1.20 (см. NOTICE). Пишет в базу
ТОЛЬКО строитель (CLI); фасад открывает её в режиме mode=ro и никогда
не конкурирует с писателем локами записи.

Наши доработки (СКД, centrality, config_maps) живут в sidecar extra.db —
см. extra_store.py; здесь только чтение чужого слоя.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

# Суффикс после ПОСЛЕДНЕЙ точки — зеркало idx_calls_callee_last (compass-44g)
CALLEE_LAST = ("substr(callee_name, length(rtrim(callee_name, "
               "replace(callee_name, '.', ''))) + 1)")


def _source_cache_limit() -> int:
    """Лимит LRU строк: env SOURCE_CACHE_LINES, дефолт 200 000; мусор/0/отрицательное — дефолт."""
    try:
        val = int(os.getenv("SOURCE_CACHE_LINES", "200000"))
    except ValueError:
        return 200_000
    return val if val > 0 else 200_000


class GyrReader:
    """Точечные выборки поверх индекса gyrfalcon.

    Параметры:
        db_path: путь к index.db (продукт `gyrfalcon build`).
    """

    def __init__(self, db_path: str | Path):
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        # check_same_thread=False + лок: FastMCP гоняет тулы в пуле потоков,
        # панель — в событийном; соединение одно, доступ сериализуется
        # (найдено контейнерным e2e — хостовые тесты однопоточные, не ловили)
        self.conn = sqlite3.connect(uri, uri=True, timeout=30,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Кэш строк файлов выгрузки (compass-1mj.8): read_method_source
        # через drvfs на тяжёлых модулях (19k строк) тормозил read_function.
        # Ключ включает mtime/size — правка файла инвалидирует сама.
        self._src_cache: OrderedDict[tuple, list[str]] = OrderedDict()
        self._src_cache_limit = _source_cache_limit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- служебное

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def stats(self) -> dict[str, int]:
        """Сводные счётчики слоёв индекса."""
        out = {}
        for table in ("methods", "modules", "calls", "object_attributes",
                      "form_elements", "subsystem_content", "metadata_references",
                      "metadata_code_usages", "role_rights", "predefined_items",
                      "register_movements", "event_subscriptions",
                      "enum_values"):
            with self._lock:
                out[table] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out

    # ------------------------------------------------------------ методы

    def find_methods(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """FTS-поиск методов (имя/текст) с местоположением."""
        return self._rows(
            """SELECT m.id, m.name, m.type, m.is_export, m.params,
                      m.line, m.end_line, mo.rel_path, mo.object_name, mo.module_type,
                      rank AS fts_rank
               FROM methods_fts f
               JOIN methods m ON m.id = f.rowid
               JOIN modules mo ON mo.id = m.module_id
               WHERE methods_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        )

    def get_method(self, name: str, module_hint: str | None = None) -> list[dict[str, Any]]:
        """Точный поиск метода по имени (безопасно: параметризовано)."""
        sql = """SELECT m.*, mo.rel_path, mo.object_name, mo.module_type
                 FROM methods m JOIN modules mo ON mo.id = m.module_id
                 WHERE m.name = ? COLLATE NOCASE"""
        params: list[Any] = [name]
        if module_hint:
            sql += " AND mo.rel_path LIKE ?"
            params.append(f"%{module_hint}%")
        sql += " ORDER BY m.is_export DESC LIMIT 50"
        return self._rows(sql, tuple(params))

    def methods_page(self, last_id: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
        """Страница методов (id > last_id, по возрастанию id) — пагинация
        для векторизации больших конфигураций без материала всего списка."""
        return self._rows(
            """SELECT m.id, m.name, m.params, m.is_export, mo.rel_path,
                      mo.object_name, mo.module_type
               FROM methods m JOIN modules mo ON mo.id = m.module_id
               WHERE m.id > ? ORDER BY m.id LIMIT ?""",
            (last_id, limit),
        )

    def callees(self, method_id: int, only_resolved: bool = False) -> list[dict[str, Any]]:
        """Что вызывает метод (исходящие рёбра с классом разрешения)."""
        extra = "AND resolution NOT IN ('unknown', 'unresolved')" if only_resolved else ""
        return self._rows(
            f"""SELECT callee_name, line, resolution, confidence
                FROM calls WHERE caller_id = ? {extra}
                ORDER BY line""",
            (method_id,),
        )

    def callers(self, method_name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Кто вызывает метод (входящие рёбра, по имени вызываемого)."""
        # F5/compass-406: точное имя; compass-44g: суффикс по ПОСЛЕДНЕЙ точке
        # (idx_calls_callee_last) — ловит «Модуль.Имя» и «Объект.Модуль.Имя»,
        # LIKE-вариант шёл полным сканом calls
        return self._rows(
            f"""SELECT c.caller_id, m.name AS caller_name, m2.rel_path, c.line,
                      c.resolution, c.confidence
               FROM calls c
               JOIN methods m ON m.id = c.caller_id
               JOIN modules m2 ON m2.id = m.module_id
               WHERE (c.callee_name = ? COLLATE NOCASE
                      OR {CALLEE_LAST} = ? COLLATE NOCASE)
               LIMIT ?""",
            (method_name, method_name, limit),
        )

    def resolve_callee(self, callee_name: str,
                       limit: int = 20) -> list[dict[str, Any]]:
        """Методы, подходящие под имя вызова: точное имя ИЛИ квалифицированное
        «Модуль.Метод» / «Объект.Модуль.Метод». Зеркально callers();
        приоритет — точное совпадение. Short-имя вычисляется в Python
        (compass-44g): «? LIKE ('%.' || m.name)» был перевёрнутым сканом
        methods; OR из двух равенств — SEARCH по idx_meth_name."""
        short = callee_name.rsplit(".", 1)[-1]
        return self._rows(
            """SELECT m.id, m.name, m.is_export, mo.rel_path, mo.object_name,
                      mo.module_type
               FROM methods m JOIN modules mo ON mo.id = m.module_id
               WHERE m.name = ? COLLATE NOCASE
                  OR m.name = ? COLLATE NOCASE
               ORDER BY CASE WHEN m.name = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                        m.is_export DESC
               LIMIT ?""",
            (callee_name, short, callee_name, limit),
        )

    def method_by_id(self, method_id: int) -> dict[str, Any] | None:
        """Метод по id с модулем (дерево вызовов и пути, compass-20c)."""
        rows = self._rows(
            """SELECT m.id, m.name, m.is_export, mo.rel_path, mo.object_name,
                      mo.module_type
               FROM methods m JOIN modules mo ON mo.id = m.module_id
               WHERE m.id = ?""",
            (method_id,))
        return rows[0] if rows else None

    def fts_columns(self) -> list[str]:
        """Колонки methods_fts (PRAGMA по виртуальной таблице). Индекс
        gyrfalcon v0.1.20 держит там только name (ddl.rs SCHEMA_FTS,
        trigram) — тел методов в FTS нет; code_grep обязан это различать
        и не имитировать поиск по коду."""
        try:
            rows = self._rows("PRAGMA table_info(methods_fts)")
        except sqlite3.OperationalError:
            return []
        return [r["name"] for r in rows]

    def creators(self, object_name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Кто создаёт объект платформенными методами, по рёбрам calls:
        callee_name «Документы.<Имя>.СоздатьДокумент», «Справочники.<Имя>.
        СоздатьЭлемент», «.<Имя>.СоздатьНаборЗаписей», «.<Имя>.Создать» —
        один LIKE «%.<Имя>.Создать%» покрывает все суффиксы. Компас-44g:
        первый предикат «суффикс начинается на Создать» ведёт выборку по
        idx_calls_callee_last (LIKE-оптимизация даёт range), второй —
        полный паттерн как раньше, фильтр уже на маленькой выборке."""
        suffix = f"%.{object_name}.Создать%"
        return self._rows(
            f"""SELECT mo.rel_path, m.name AS caller, c.callee_name, c.line,
                      c.resolution, c.confidence
               FROM calls c
               JOIN methods m ON m.id = c.caller_id
               JOIN modules mo ON mo.id = m.module_id
               WHERE {CALLEE_LAST} LIKE 'Создать%' COLLATE NOCASE
                     AND c.callee_name LIKE ? COLLATE NOCASE
               ORDER BY c.line
               LIMIT ?""",
            (suffix, limit),
        )

    def freshness_snapshot(self) -> dict[str, Any]:
        """Срез свежести: max(modules.mtime), built_at из index_meta,
        счётчик модулей (для index_freshness). mtime строитель не
        заполняет (сверено с ddl.rs v0.1.20) — на живых индексах None."""
        rows = self._rows(
            "SELECT MAX(mtime) AS max_mtime, COUNT(*) AS total FROM modules")
        return {"max_module_mtime": rows[0]["max_mtime"],
                "modules_total": rows[0]["total"],
                "built_at": self.index_meta_value("built_at")}

    def read_method_source(self, rel_path: str, line_start: int, line_end: int,
                           source_root: str | Path) -> str:
        """Тело метода из исходника (в индексе тел нет — так задуман gyrfalcon).

        source_root — корень выгрузки, соответствующей индексу.
        """
        lines = self.source_lines(rel_path, source_root)
        return "\n".join(lines[line_start - 1 : line_end])

    def source_lines(self, rel_path: str, source_root: str | Path) -> list[str]:
        """Строки файла выгрузки с кэшем (compass-1mj.8): ключ
        (source_root, rel_path, mtime, size) → list[str]; повторное чтение
        не трогает диск, правка файла меняет mtime/size — ключ новый, файл
        перечитывается (инвалидация автоматическая). Лимит кэша —
        SOURCE_CACHE_LINES строк (LRU); файл крупнее лимита не кэшируется,
        чтобы один тяжёлый модуль не вытеснял весь кэш. Файл не читается
        или нет — [] (read_method_source вернёт "" как раньше).
        """
        path = Path(source_root) / rel_path
        try:
            st = path.stat()
        except OSError:
            return []
        key = (str(Path(source_root)), rel_path, st.st_mtime, st.st_size)
        with self._lock:
            cached = self._src_cache.get(key)
            if cached is not None:
                self._src_cache.move_to_end(key)
                return cached
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return []
        lines = text.splitlines()
        if len(lines) <= self._src_cache_limit:
            self._src_cache_put(key, lines)
        return lines

    def _src_cache_put(self, key: tuple, lines: list[str]) -> None:
        """Положить срез в LRU и вытеснять старые, пока не влезет в лимит.
        Под RLock: read_method_source зовётся из пула потоков FastMCP,
        OrderedDict не потокобезопасен (тот же лок, что у соединения).
        """
        with self._lock:
            self._src_cache[key] = lines
            self._src_cache.move_to_end(key)
            total = sum(len(v) for v in self._src_cache.values())
            while total > self._src_cache_limit and len(self._src_cache) > 1:
                _, evicted = self._src_cache.popitem(last=False)
                total -= len(evicted)

    def index_meta_value(self, key: str) -> str | None:
        """Значение из index_meta (source_path/built_at). Таблицы нет или
        ключа нет — None (мини-фикстура с compass-kep держит index_meta
        пустой — состав колонок теперь под метатестом test_ddl_meta)."""
        try:
            rows = self._rows("SELECT value FROM index_meta WHERE key = ?",
                              (key,))
        except sqlite3.OperationalError:
            return None
        return rows[0]["value"] if rows else None

    def changed_modules(self, source_root: str | Path) -> dict[str, Any]:
        """Diff выгрузки против таблицы modules: изменённые и удалённые
        файлы для gyrfalcon update (compass-941.4).

        Сверено с ddl.rs v0.1.20: у modules есть mtime REAL и size
        INTEGER, но строитель mtime НЕ заполняет (build.rs/incremental.rs
        пишут только size) — основа сравнения size, mtime участвует когда
        есть. Оба признака NULL (у живого строителя не бывает) — файл
        считается изменённым: нечем сверить, перестраховка дешевле
        молча расходящегося индекса. rel_path слоёв расширений с '../ext/'
        — Path через ОС разрешает. НОВЫЕ файлы (ещё не в modules) дифф
        не видит — их подберёт полная сборка или update без списка
        файлов.
        """
        root = Path(source_root)
        changed: list[str] = []
        deleted: list[str] = []
        unchanged = 0
        for row in self._rows("SELECT rel_path, mtime, size FROM modules"):
            rel = row["rel_path"]
            path = root / rel
            if not path.is_file():
                deleted.append(rel)
                continue
            st = path.stat()
            size_known = row["size"] is not None
            mtime_known = row["mtime"] is not None
            same = (
                (not size_known or int(row["size"]) == st.st_size)
                and (not mtime_known
                     or abs(float(row["mtime"]) - st.st_mtime) < 1e-6)
            )
            if not size_known and not mtime_known:
                same = False
            if same:
                unchanged += 1
            else:
                changed.append(rel)
        return {"changed": changed, "deleted": deleted, "unchanged": unchanged,
                "checked": unchanged + len(changed) + len(deleted)}

    # -------------------------------------------------------- метаданные

    def object_attributes(self, object_name: str) -> list[dict[str, Any]]:
        """Реквизиты объекта (включая ТЧ) с типами; category — вид метаданных
        (compass-1hh: одинаковые имена разных видов различаются ею)."""
        return self._rows(
            """SELECT category, attr_name, attr_synonym, attr_type, attr_kind,
                      ts_name
               FROM object_attributes WHERE object_name = ? COLLATE NOCASE
               ORDER BY attr_kind, attr_name""",
            (object_name,),
        )

    def movements(self, document_name: str) -> list[dict[str, Any]]:
        """Движения документа по регистрам (наш triggers_on_write, часть 1)."""
        return self._rows(
            "SELECT register_name, source FROM register_movements "
            "WHERE document_name = ? COLLATE NOCASE",
            (document_name,),
        )

    def event_subscriptions(self, source_object: str | None = None) -> list[dict[str, Any]]:
        """Подписки на события (наш triggers_on_write, часть 2)."""
        if source_object:
            return self._rows(
                "SELECT name, event, handler_module, handler_procedure, source_types "
                "FROM event_subscriptions WHERE source_types LIKE ?",
                (f"%{source_object}%",),
            )
        return self._rows(
            "SELECT name, event, handler_module, handler_procedure, source_types "
            "FROM event_subscriptions"
        )

    def subsystems_of(self, object_name: str) -> list[dict[str, Any]]:
        """Подсистемы, содержащие объект (прямое вхождение); object_ref —
        «ВидЕдинств.Имя» (compass-1hh: фильтр по виду в фасаде)."""
        return self._rows(
            """SELECT subsystem_name, subsystem_synonym, object_ref
               FROM subsystem_content WHERE object_ref LIKE ?""",
            (f"%{object_name}%",),
        )

    def enum_values(self, object_name: str) -> dict[str, Any] | None:
        """Перечисление: имя, синоним, значения (values_json из enum_values).

        Ограничение индекса: мета сохраняет только ИМЕНА значений
        (meta.rs:353) — синонимы значений не доступны (F3/compass-1v3).
        None — объект не перечисление (нет строки в enum_values).
        """
        rows = self._rows(
            "SELECT name, synonym, values_json FROM enum_values "
            "WHERE name = ? COLLATE NOCASE LIMIT 1",
            (object_name,))
        if not rows:
            return None
        try:
            values = json.loads(rows[0]["values_json"] or "[]")
        except json.JSONDecodeError:
            values = []
        return {"name": rows[0]["name"],
                "synonym": rows[0]["synonym"] or "",
                "values": values, "total": len(values)}

    def forms_of(self, object_name: str) -> list[dict[str, Any]]:
        """Формы объекта: элементы/обработчики (срез по формам)."""
        return self._rows(
            """SELECT form_name, COUNT(*) AS elements,
                      COUNT(DISTINCT handler) AS handlers
               FROM form_elements WHERE object_name = ? COLLATE NOCASE
               GROUP BY form_name ORDER BY elements DESC""",
            (object_name,),
        )

    def find_in_forms(self, what: str, limit: int = 30) -> list[dict[str, Any]]:
        """Где в формах встречается имя (элемент/обработчик/команда)."""
        return self._rows(
            """SELECT object_name, form_name, kind, element_name, event, handler
               FROM form_elements
               WHERE element_name LIKE ? OR handler LIKE ?
               LIMIT ?""",
            (f"%{what}%", f"%{what}%", limit),
        )

    def references_to(self, object_name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Кто ссылается на объект реквизитами/в коде (влияние)."""
        return self._rows(
            """SELECT source_object, source_category, ref_kind, used_in, path, line
               FROM metadata_references
               WHERE ref_object LIKE ? LIMIT ?""",
            (f"%{object_name}%", limit),
        )

    def code_usages(self, object_name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Обращения к объекту из кода (кто трогает данные в модулях)."""
        return self._rows(
            """SELECT mo.rel_path, u.member_path, u.usage_kind, u.line
               FROM metadata_code_usages u
               JOIN modules mo ON mo.id = u.module_id
               WHERE u.object_ref LIKE ? LIMIT ?""",
            (f"%{object_name}%", limit),
        )

    def role_rights(self, object_name: str) -> list[dict[str, Any]]:
        """Права ролей на объект."""
        return self._rows(
            "SELECT role_name, right_name FROM role_rights "
            "WHERE object_name = ? OR object_name LIKE ?",
            (object_name, f"%.{object_name}"),
        )

    def predefined(self, object_name: str) -> list[dict[str, Any]]:
        """Предопределённые элементы справочника/перечисления."""
        return self._rows(
            "SELECT item_name, item_synonym, item_code, is_folder "
            "FROM predefined_items WHERE object_name = ? COLLATE NOCASE",
            (object_name,),
        )

    def entry_points(self) -> dict[str, list[dict[str, Any]]]:
        """Точки входа: регл. задания, веб- и HTTP-сервисы."""
        return {
            "scheduled_jobs": self._rows(
                "SELECT name, method_name, handler_module, handler_procedure, use "
                "FROM scheduled_jobs"
            ),
            "web_services": self._rows(
                "SELECT name, namespace FROM web_services"
            ),
            "http_services": self._rows(
                "SELECT name, root_url FROM http_services"
            ),
        }

    def extension_overrides(self) -> list[dict[str, Any]]:
        """Перехваты расширений (наш роадмап F1 — данные в схеме из коробки;
        на дампе без расширений будет пусто)."""
        return self._rows(
            "SELECT object_name, target_method, target_method_line, extension_name "
            "FROM extension_overrides LIMIT 500"
        )

    def extensions_overview(self) -> dict[str, Any]:
        """Обзор расширений: список + счётчики заимствований/перехватов."""
        exts = self._rows(
            "SELECT name, purpose, name_prefix FROM extensions")
        counts = {r["extension_name"]: r["n"] for r in self._rows(
            """SELECT extension_name, COUNT(*) AS n
               FROM extension_overrides GROUP BY extension_name""")}
        borrowed = {r["extension_name"]: r["n"] for r in self._rows(
            """SELECT extension_name, COUNT(*) AS n
               FROM extension_objects
               WHERE belonging IN ('borrowed', 'adopted')
               GROUP BY extension_name""")}
        for e in exts:
            e["overrides"] = counts.get(e["name"], 0)
            e["borrowed"] = borrowed.get(e["name"], 0)
        total = self.conn.execute(
            "SELECT COUNT(*) FROM extension_objects").fetchone()[0]
        return {"extensions": exts, "objects_total": total,
                "overrides_total": sum(counts.values())}

    def overrides_for(self, object_name: str) -> list[dict[str, Any]]:
        """Перехваты и принадлежность объектов одного имени (расширение/база)."""
        suffix = f"%.{object_name}"  # F5: конкатенация вне скана строк
        overrides = self._rows(
            """SELECT object_name, target_method, target_method_line,
                      extension_name, annotation
               FROM extension_overrides
               WHERE object_name = ? OR object_name LIKE ?
               LIMIT 200""",
            (object_name, suffix))
        belongings = self._rows(
            """SELECT object_name, category, belonging, extension_name
               FROM extension_objects
               WHERE object_name = ? OR object_name LIKE ?
               LIMIT 200""",
            (object_name, suffix))
        return {"overrides": overrides, "objects": belongings}

    def overrides_of_method(self, method_name: str) -> list[dict[str, Any]]:
        """Все перехваты метода по имени цели: extension_overrides × методы
        перехватчиков через source_module_id (линии/путь перехватчика);
        тело — по extension_method (имя в модуле расширения)."""
        return self._rows(
            """SELECT o.object_name, o.target_method, o.target_method_line,
                      o.annotation, o.extension_name, o.extension_purpose,
                      o.extension_method, o.source_path, o.ext_module_path,
                      m.id AS method_id, m.name AS intercept_name,
                      m.type AS intercept_type,
                      m.line AS intercept_line,
                      m.end_line AS intercept_end_line,
                      mo.rel_path AS intercept_path
               FROM extension_overrides o
               LEFT JOIN methods m ON m.id = o.source_module_id
               LEFT JOIN modules mo ON mo.id = m.module_id
               WHERE o.target_method = ? COLLATE NOCASE
               ORDER BY o.id""",
            (method_name,))

    def extension_counts(self) -> list[dict[str, Any]]:
        """Список расширений индекса: имя, назначение, префикс, перехватов,
        заимствовано/своих (extension_objects). belonging толерантен к
        терминологии: 'borrowed' (прежде/reader) и 'adopted' (DDL) — оба
        считаются заимствованием."""
        return self._rows(
            """SELECT e.name, e.purpose, e.name_prefix,
                      (SELECT COUNT(*) FROM extension_overrides o
                        WHERE o.extension_name = e.name COLLATE NOCASE) AS overrides,
                      (SELECT COUNT(*) FROM extension_objects x
                        WHERE x.extension_name = e.name COLLATE NOCASE
                          AND x.belonging IN ('borrowed', 'adopted')) AS borrowed,
                      (SELECT COUNT(*) FROM extension_objects x
                        WHERE x.extension_name = e.name COLLATE NOCASE
                          AND x.belonging = 'own') AS own
               FROM extensions e ORDER BY e.name""")

    # ------------------------------------------------- справка (help corpus)

    def help_sources(self) -> list[dict[str, Any]]:
        """Подключённые help-источники со счётчиками страниц и объёмом."""
        return self._rows(
            """SELECT s.source_id, s.root,
                      COUNT(p.id) AS pages,
                      COALESCE(SUM(p.chars), 0) AS chars
               FROM help_sources s
               LEFT JOIN help_pages p ON p.source_id = s.source_id
               GROUP BY s.source_id, s.root ORDER BY s.source_id""")

    def help_search(self, query: str, source_id: str | None = None,
                    limit: int = 10) -> list[dict[str, Any]]:
        """FTS5-поиск по справке (title/object_name/text) со сниппетом.

        query — синтаксис FTS5 (голые слова = И; «серии парти*» — префиксы);
        MATCH при невалидном синтаксисе кидает OperationalError — фасад
        переводит это в честный _no_matches с хинтом.
        """
        sql = """SELECT p.source_id, p.path, p.title, p.object_name,
                        snippet(help_fts, 2, '«', '»', '…', 24) AS snippet,
                        rank AS fts_rank
                 FROM help_fts f
                 JOIN help_pages p ON p.id = f.rowid
                 WHERE help_fts MATCH ?"""
        params: list[Any] = [query]
        if source_id:
            sql += " AND p.source_id = ?"
            params.append(source_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return self._rows(sql, tuple(params))

    def help_page(self, source_id: str, path: str) -> list[dict[str, Any]]:
        """Полный текст страницы справки (source_id + path, как в поиске)."""
        return self._rows(
            """SELECT source_id, path, title, category, object_name,
                      chars, text
               FROM help_pages WHERE source_id = ? AND path = ?""",
            (source_id, path))

    def object_names(self, limit: int = 20000) -> list[str]:
        """Имена объектов метаданных (для mentions-матчинга «дока ↔ код»)."""
        rows = self._rows(
            """SELECT DISTINCT object_name FROM object_attributes
               UNION SELECT DISTINCT object_name FROM modules
               ORDER BY 1 LIMIT ?""",
            (limit,),
        )
        return [r["object_name"] for r in rows if r["object_name"]]
