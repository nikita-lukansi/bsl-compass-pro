# -*- coding: utf-8 -*-
"""Тесты migrate-to-homes (S4, arch-storage v0.2 §4) на синтетическом
старом layout: legacy source_root + index.db в tmp + help-корпус в
legacy-точке; перенос в дом configs/<cid>, бэкап реестра, сверка."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg_mod  # noqa: E402
from src import workspace as ws  # noqa: E402

SYMLINK_OK = True
try:
    _probe = Path("/tmp") / "_bsl_link_probe_s4"
    if _probe.exists():
        _probe.unlink()
    import os
    os.symlink(_probe.parent, _probe, target_is_directory=True)
    _probe.unlink()
except (OSError, NotImplementedError):
    SYMLINK_OK = False

needs_symlink = pytest.mark.skipif(
    not SYMLINK_OK, reason="os.symlink недоступен (привилегии/фс)")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Реестр и хабы в tmp; legacy-точка Help — тоже tmp."""
    settings_dir = tmp_path / "data" / "system" / "settings"
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_CONFIGS_ROOT", str(tmp_path / "data" / "configs"))
    monkeypatch.setenv("COMPASS_DOCS_ROOT", str(tmp_path / "data" / "docs"))
    monkeypatch.setenv("COMPASS_EXTENSIONS_ROOT",
                       str(tmp_path / "data" / "extensions"))
    monkeypatch.setattr(ws, "root", lambda: tmp_path / "workspace")
    # legacy-точка host-CLI краулера — синтетическая (не реальный /workspace)
    monkeypatch.setenv("WORKSPACE_HUB", str(tmp_path / "workspace"))
    monkeypatch.delenv("COMPASS_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("COMPASS_INDEX_DB", raising=False)
    ws._TEXT_CACHE.clear()
    ws.init_files()
    return tmp_path


def _mini_index(path: Path) -> None:
    """Минимальный индекс с help_pages-таблицей (reader открывает)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT,
        category TEXT, object_name TEXT, module_type TEXT, form_name TEXT,
        is_form INTEGER, mtime REAL, size INTEGER);
    INSERT INTO modules (rel_path, category, object_name, module_type,
        form_name, is_form, mtime, size)
        VALUES ('Catalogs/N/obj.bsl', 'Catalogs', 'N', 'ObjectModule', '',
                0, 100.0, 10);
    """)
    conn.commit()
    conn.close()


def _legacy_config(homes, short="ut", with_index=True, with_help=True):
    """Старый layout: дамп в workspace/Configurations, индекс в legacy,
    help-корпус в /workspace/Help/ut11doc."""
    src = homes / "workspace" / "Configurations" / short
    src.mkdir(parents=True)
    (src / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    (src / "Модуль.bsl").write_text("П", encoding="utf-8")

    old_index = homes / "data" / "index" / short / "index.db"
    if with_index:
        _mini_index(old_index)

    if with_help:
        corpus = homes / "workspace" / "Help" / "ut11doc"
        corpus.mkdir(parents=True)
        (corpus / "page.html").write_text("<h>", encoding="utf-8")

    out = reg_mod.add_config(short, str(src))
    cid = out["id"]
    reg = reg_mod.load_registry()
    reg["configs"][cid]["index_db"] = str(old_index)
    reg_mod.save_registry(reg)
    if with_help and with_index:
        # reader.help_sources возвращает source_id корпуса — пишем таблицу
        conn = sqlite3.connect(old_index)
        conn.executescript("""
        CREATE TABLE help_pages (id INTEGER PRIMARY KEY, source_id TEXT,
            path TEXT, chars INTEGER);
        CREATE TABLE help_sources (source_id TEXT, root TEXT);
        INSERT INTO help_sources VALUES ('ut11doc', '/workspace/Help/ut11doc');
        INSERT INTO help_pages (source_id, path, chars) VALUES
            ('ut11doc', 'a/b', 100);
        """)
        conn.commit()
        conn.close()
    return cid


# ------------------------------------------------------------------- базовый

def test_migrate_legacy_to_home(homes):
    cid = _legacy_config(homes)
    out = reg_mod.migrate_to_homes()
    assert out["migrated"] == [cid]
    assert not out["errors"]
    backup = Path(out["backup"])
    assert backup.exists() and "bak-" in backup.name

    entry = reg_mod.get_config(cid)
    new_source = reg_mod.config_source_root(cid)
    new_index = reg_mod.config_index_db(cid)
    # реестр — дом-пути
    assert entry["source_root"] == str(new_source)
    assert entry["index_db"] == str(new_index)
    assert entry["source_version"] == reg_mod.source_version(new_source)
    # index.db ПЕРЕНЕСЁН (по одному экземпляру), source СКОПИРОВАН
    assert new_index.exists()
    assert not (homes / "data" / "index" / cid_for(homes, "ut") /
                "index.db").exists()
    assert (new_source / "Configuration.xml").exists()
    # help-корпус переехал в docs-хаб
    corpus = reg_mod.config_docs_root(cid, "ut11doc")
    assert (corpus / "page.html").exists()
    assert not (homes / "workspace" / "Help" / "ut11doc").exists()
    # сверка
    assert out["checks"][cid]["source"] is True
    assert out["checks"][cid]["index"] is True
    assert out["checks"][cid]["reader_opens"] is True


def cid_for(homes, short):
    for cid, e in reg_mod.load_registry()["configs"].items():
        if e["short_name"] == short:
            return cid
    return ""


def test_migrate_skips_home_scheme(homes):
    """Уже дом-конфигурация (S5 add) — skipped, файлы не тронуты."""
    out = reg_mod.add_config("ka")
    cid = out["id"]
    out2 = reg_mod.migrate_to_homes()
    assert [s["cid"] for s in out2["skipped"]] == [cid]
    assert not out2["migrated"] and not out2["errors"]


def test_migrate_without_index_and_help(homes):
    """Индекса/корпусов нет: пути обновляются, сборка создаст индекс."""
    cid = _legacy_config(homes, short="zp", with_index=False, with_help=False)
    out = reg_mod.migrate_to_homes(cid)
    assert out["migrated"] == [cid]
    entry = reg_mod.get_config(cid)
    assert entry["index_db"] == str(reg_mod.config_index_db(cid))
    checks = out["checks"][cid]
    assert checks["index"] is False      # сверка честная: индекса нет
    assert "reader_opens" not in checks


def test_migrate_single_cid(homes):
    """cid-фильтр: мигрирует только указанная конфигурация."""
    cid1 = _legacy_config(homes, short="ka", with_help=False)
    cid2 = _legacy_config(homes, short="ut7", with_help=False)
    out = reg_mod.migrate_to_homes(cid1)
    assert out["migrated"] == [cid1]
    assert reg_mod.get_config(cid2)["source_root"] == str(
        homes / "workspace" / "Configurations" / "ut7")


@needs_symlink
def test_migrate_ext_link(homes):
    """Расширяемая конфигурация: после миграции симлинк source/ext стоит."""
    cid = _legacy_config(homes, short="ep")
    reg_mod.stack_add(cid, "Расш1", 1, "Исправление")
    out = reg_mod.migrate_to_homes(cid)
    assert out["migrated"] == [cid]
    assert out["checks"][cid]["ext_link"] == "симлинк создан"
    assert (reg_mod.config_source_root(cid) / "ext").is_symlink()
