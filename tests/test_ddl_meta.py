# -*- coding: utf-8 -*-
"""Метатест DDL (TEST-3, compass-kep): мини-фикстура синхронна ddl.rs.

Мини-индекс существует, чтобы SQL reader'а гонялся на герметичных данных.
Если колонку, которую ddl.rs создаёт в живом индексе, мини не имеет —
тесты молча не покрывают путь, который на живом индексе упадёт (или,
наоборот, живая колонка остаётся без покрытых запросов). Метатест
сверяет колонки (имена И порядок — mini использует позиционные вставки)
для таблиц, которые читает GyrReader, и падает со списком расхождений.

За пределами сверы (сознательно):
- help-таблицы help_pages/help_sources/help_fts — их DDL в help.rs,
  не в ddl.rs (мини сверял их с help.rs вручную, см. комментарий в
  mini_index.py);
- виртуальные FTS-таблицы — состав колонок и токенизаторы мини намеренно
  отличаются (см. докстринг mini_index.py);
- индексы — зеркало индексов мини ручное (compass-44g), метатестом не
  сверяются.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.mini_index import make_mini_db  # noqa: E402

_ROOT = Path(__file__).parent.parent
_DDL = _ROOT / "vendored/gyrfalcon/crates/gyrfalcon-index/src/ddl.rs"
_READER = _ROOT / "src/recon/gyr_reader.py"

# вне ddl-сверки: FTS-таблицы и help-слой (DDL в help.rs) — см. докстринг
_SKIP = {"methods_fts", "objects_fts", "help_fts", "help_pages", "help_sources"}


def _ddl_columns(text: str) -> dict[str, list[str]]:
    """CREATE TABLE IF NOT EXISTS <t> (...): имена колонок в порядке ddl.rs."""
    tables: dict[str, list[str]] = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);",
                                 text, re.S):
        cols = []
        for part in body.split(","):
            token = part.strip().split()
            if token and token[0].upper() not in ("PRIMARY", "UNIQUE",
                                                  "FOREIGN", "CHECK",
                                                  "CONSTRAINT"):
                cols.append(token[0])
        tables[name] = cols
    return tables


def _reader_tables() -> list[str]:
    """Таблицы из SQL gyr_reader.py (FROM/JOIN), в порядке первого упоминания.

    Список выводится из исходника, а не хардкодится: таблица появилась в
    reader'е — метатест подхватит её сам."""
    text = _READER.read_text(encoding="utf-8")
    names = re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)", text)
    return [t for t in dict.fromkeys(names) if t not in _SKIP]


@pytest.fixture
def mini(tmp_path):
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_mini_schema_matches_ddl(mini):
    """Колонки каждой читаемой GyrReader таблицы мини == ddl.rs:
    имена и порядок; типы мини могут отличаться намеренно (докстринг
    mini_index.py). Расхождение → один fail со списком по каждой таблице."""
    ddl_tables = _ddl_columns(_DDL.read_text(encoding="utf-8"))
    checked = [t for t in _reader_tables() if t in ddl_tables]
    assert checked, "парсер ddl.rs сломался — ни одной сверяемой таблицы"
    problems: list[str] = []
    for table in checked:
        got = [r["name"] for r in mini.execute(f"PRAGMA table_info({table})")]
        want = ddl_tables[table]
        if not got:
            problems.append(f"{table}: таблицы НЕТ в мини; ddl: {want}")
            continue
        only_ddl = [c for c in want if c not in got]
        only_mini = [c for c in got if c not in want]
        if only_ddl or only_mini or got != want:
            problems.append(
                f"{table}: только в ddl: {only_ddl}; только в мини: {only_mini}; "
                f"порядок мини {got} != ddl {want}")
    assert not problems, (
        "мини-схема разошлась с ddl.rs (сверь и выровняй mini_index.py):\n"
        + "\n".join(problems))
