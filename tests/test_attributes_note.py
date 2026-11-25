# -*- coding: utf-8 -*-
"""compass-a49: attributes_note строится от фактического среза.

Было: «первые 50 из 28» при total=28 — текст пагинации формировался
безусловно. Контракт: срез есть только когда total > 50 — тогда note
с честным total; иначе note не нужен вовсе.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


def _db_with_attrs(tmp_path: Path, object_name: str, attrs: int) -> Path:
    db = make_mini_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO object_attributes (id, object_name, category, attr_name,"
        " attr_synonym, attr_type, attr_kind, ts_name, source_file, length,"
        " precision) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'f.xml', 0, 0)",
        ((9000 + i, object_name, "Documents", f"Реквизит{i:03d}",
          None, '["String"]', 'attribute')
         for i in range(attrs)))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def scaled(monkeypatch, tmp_path):
    """60 реквизитов у одного объекта — срез срабатывает."""
    db = _db_with_attrs(tmp_path, "ШирокийОбъект", 60)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


@pytest.fixture
def mini(monkeypatch, tmp_path):
    """Мини-индекс + чистый кэш читателя (как в test_facade_tools)."""
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield db
    tools.reset_reader()


def test_note_present_and_honest_when_sliced(scaled):
    out = tools.object_overview("ШирокийОбъект")
    assert out["attributes_total"] == 60
    assert len(out["attributes"]) == 50
    assert out["attributes_note"] == (
        "первые 50 из 60; kinds — точные счётчики, ТЧ — в table_sections, "
        "точная проверка — verify_field")


def test_kinds_counted_over_full_set_not_slice(scaled):
    """kinds — точные счётчики по всем 60, а не по выданным 50 (комментарий
    в старом note обещал это — теперь проверено)."""
    out = tools.object_overview("ШирокийОбъект")
    assert out["attributes_kinds"] == {"attribute": 60}


def test_note_absent_when_no_slice(mini, monkeypatch):
    """total ≤ 50: note нет вовсе (a49: «первые 50 из 28» — недопустимо)."""
    out = tools.object_overview("ОбъектТест")
    assert out["attributes_total"] <= 50
    assert "attributes_note" not in out
