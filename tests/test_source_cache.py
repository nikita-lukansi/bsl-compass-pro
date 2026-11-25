# -*- coding: utf-8 -*-
"""compass-1mj.8: LRU-кэш строк файлов выгрузки в GyrReader.

Ключ (source_root, rel_path, mtime, size): повторное чтение не зовёт
read_text (тяжёлый модуль через drvfs не перечитывается); правка файла
(mtime/size) меняет ключ — файл перечитывается; SOURCE_CACHE_LINES —
лимит строк LRU, файл крупнее лимита не кэшируется вовсе.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.recon.gyr_reader import GyrReader  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """Мини-выгрузка (два модуля) + валидный index.db для GyrReader."""
    src = tmp_path / "src"
    mod = src / "CommonModules/Мод/Ext/Module.bsl"
    mod.parent.mkdir(parents=True)
    mod.write_text("строка 1\nстрока 2\nстрока 3\n", encoding="utf-8-sig")
    other = src / "Documents/Док/Ext/ManagerModule.bsl"
    other.parent.mkdir(parents=True)
    other.write_text("а\nб\nв\n", encoding="utf-8-sig")
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    r = GyrReader(db)
    yield {"reader": r, "src": src, "file": mod, "other": other, "db": db}
    r.close()


def _counting_read_text(monkeypatch) -> list[str]:
    """Path.read_text со счётчиком вызовов (как требует ТЗ задачи)."""
    calls: list[str] = []
    real = Path.read_text

    def counting(self, *args, **kwargs):
        calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return calls


def test_repeat_read_hits_cache(env, monkeypatch):
    r, src = env["reader"], env["src"]
    rel = env["file"].relative_to(src).as_posix()
    calls = _counting_read_text(monkeypatch)
    body1 = r.read_method_source(rel, 1, 3, src)
    body2 = r.read_method_source(rel, 1, 3, src)
    assert body1 == body2 == "строка 1\nстрока 2\nстрока 3"
    assert len(calls) == 1, "второе чтение обязано браться из кэша"
    assert len(r.source_lines(rel, src)) == 3
    assert len(calls) == 1


def test_mtime_change_reloads(env, monkeypatch):
    """Инвалидация по mtime: utime — ключ новый, файл перечитан."""
    r, src, f = env["reader"], env["src"], env["file"]
    rel = f.relative_to(src).as_posix()
    calls = _counting_read_text(monkeypatch)
    assert r.read_method_source(rel, 1, 3, src)
    future = f.stat().st_mtime + 10
    os.utime(f, (future, future))
    assert r.read_method_source(rel, 1, 3, src)
    assert len(calls) == 2, "смена mtime обязана перечитать файл"


def test_size_change_reloads(env, monkeypatch):
    """Инвалидация по size: перезапись с другим объёмом — перечитывание."""
    r, src, f = env["reader"], env["src"], env["file"]
    rel = f.relative_to(src).as_posix()
    calls = _counting_read_text(monkeypatch)
    assert r.read_method_source(rel, 1, 3, src)
    f.write_text("строка 1\nстрока 2\nстрока 3\nстрока 4\n",
                 encoding="utf-8-sig")
    body = r.read_method_source(rel, 1, 4, src)
    assert "строка 4" in body
    assert len(calls) == 2, "смена size обязана перечитать файл"


def test_lru_evicts_least_recently_used(env, monkeypatch):
    """Лимит 5 строк при модулях по 3: вставка второго вытесняет первый —
    повторное чтение первого идёт на диск, второго — из кэша."""
    monkeypatch.setenv("SOURCE_CACHE_LINES", "5")
    src, db = env["src"], env["db"]
    r = GyrReader(db)          # лимит читается в __init__ — после setenv
    try:
        calls = _counting_read_text(monkeypatch)
        rel1 = env["file"].relative_to(src).as_posix()
        rel2 = env["other"].relative_to(src).as_posix()
        assert r.source_lines(rel1, src)      # первый в кэше
        assert r.source_lines(rel2, src)      # 3+3 > 5 — вытесняет первого
        assert r.source_lines(rel2, src)      # второй — из кэша
        assert r.source_lines(rel1, src)      # первый перечитывается с диска
        assert len(calls) == 3, "rel1 перечитан (вытеснен), rel2 — из кэша"
        rel1_reads = sum(1 for c in calls
                         if c.replace("\\", "/").endswith("Мод/Ext/Module.bsl"))
        assert rel1_reads == 2, "перечитанное — именно вытесненный rel1"
    finally:
        r.close()


def _db_path(src: Path) -> Path:
    """Рядом с выгрузкой: валидный пустой index.db для GyrReader."""
    db = src.parent / "index.db"
    if not db.exists():
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
    return db


def test_oversized_file_not_cached(env, monkeypatch):
    """Файл крупнее лимита не кэшируется: один тяжёлый модуль не должен
    вытеснять весь кэш — чтение каждый раз с диска, честно."""
    monkeypatch.setenv("SOURCE_CACHE_LINES", "2")
    src = env["src"]
    r = GyrReader(_db_path(src))
    try:
        calls = _counting_read_text(monkeypatch)
        rel = env["file"].relative_to(src).as_posix()
        assert r.read_method_source(rel, 1, 3, src)
        assert r.read_method_source(rel, 1, 3, src)
        assert len(calls) == 2
        assert r._src_cache == {}
    finally:
        r.close()


def test_limit_env_garbage_falls_back_to_default(env, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE_LINES", "абв")
    r = GyrReader(_db_path(env["src"]))
    try:
        assert r._src_cache_limit == 200_000
    finally:
        r.close()
    monkeypatch.setenv("SOURCE_CACHE_LINES", "0")
    r2 = GyrReader(_db_path(env["src"]))
    try:
        assert r2._src_cache_limit == 200_000
    finally:
        r2.close()


def test_missing_file_is_empty(env):
    r = env["reader"]
    assert r.source_lines("Нет/Такого.bsl", env["src"]) == []
    assert r.read_method_source("Нет/Такого.bsl", 1, 2, env["src"]) == ""
