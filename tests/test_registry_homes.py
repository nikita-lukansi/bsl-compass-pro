# -*- coding: utf-8 -*-
"""Тесты дом-схемы хранения (S1, arch-storage v0.2): хабы configs/docs/
extensions, доменные дефолты add_config, source_version, симлинк ext.

Существующие (legacy) записи не мигрируются здесь — их переносит S4.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg_mod  # noqa: E402
from src import workspace as ws  # noqa: E402

SYMLINK_OK = True
try:
    _probe = Path(os.environ.get("TEMP", "/tmp")) / "_bsl_link_probe"
    if _probe.exists():
        _probe.unlink()
    os.symlink(_probe.parent, _probe, target_is_directory=True)
    _probe.unlink()
except (OSError, NotImplementedError):
    SYMLINK_OK = False

needs_symlink = pytest.mark.skipif(
    not SYMLINK_OK, reason="os.symlink недоступен (привилегии/фс)")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Реестр и три хаба в tmp; env-пути сборки убраны."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_CONFIGS_ROOT", str(tmp_path / "data" / "configs"))
    monkeypatch.setenv("COMPASS_DOCS_ROOT", str(tmp_path / "data" / "docs"))
    monkeypatch.setenv("COMPASS_EXTENSIONS_ROOT",
                       str(tmp_path / "data" / "extensions"))
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB"):
        monkeypatch.delenv(var, raising=False)
    ws._TEXT_CACHE.clear()
    return tmp_path / "data"


def _dump(root: Path, files: dict[str, int]) -> Path:
    """Мини-выгрузка: Configuration.xml + файлы заданных размеров."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    for name, size in files.items():
        (root / name).write_text("x" * size, encoding="utf-8")
    return root


# ------------------------------------------------------------ хабы/дефолты

def test_home_helpers(homes):
    assert reg_mod.configs_root() == homes / "configs"
    assert reg_mod.config_home("Compass_k_abc") == homes / "configs" / "Compass_k_abc"
    assert reg_mod.config_index_db("Compass_k_abc") == \
        homes / "configs" / "Compass_k_abc" / "index.db"
    assert reg_mod.config_source_root("Compass_k_abc") == \
        homes / "configs" / "Compass_k_abc" / "source"
    assert reg_mod.config_docs_root("Compass_k_abc", "ut11doc") == \
        homes / "docs" / "Compass_k_abc" / "ut11doc"
    assert reg_mod.config_extension_root("Compass_k_abc", "Расш1") == \
        homes / "extensions" / "Compass_k_abc" / "Расш1"


def test_add_config_defaults_to_home(homes):
    """add_config без source_root: дом-дефолты index.db и source/."""
    out = reg_mod.add_config("ut")
    cid = out["id"]
    assert "error" not in out
    assert Path(out["entry"]["index_db"]) == reg_mod.config_index_db(cid)
    assert Path(out["entry"]["source_root"]) == reg_mod.config_source_root(cid)
    assert out["entry"]["source_version"] is None  # source ещё не скопирован


def test_add_config_explicit_source_stays_legacy(homes):
    """Явный source_root (панель сегодня) — пути как переданы, не дом."""
    src = homes.parent / "host_dump"
    _dump(src, {"a.bsl": 10})
    out = reg_mod.add_config("ka", str(src))
    cid = out["id"]
    assert out["entry"]["source_root"] == str(src)
    assert out["entry"]["source_version"] == reg_mod.source_version(src)
    # legacy-запись: симлинк не создаётся (пути не дом-схемы)
    assert "skipped" in reg_mod.ensure_ext_link(cid)


def test_source_version_changes_with_tree(homes):
    src = homes / "src1"
    _dump(src, {"a.bsl": 10})
    v1 = reg_mod.source_version(src)
    assert v1 and len(v1) == 64
    (src / "a.bsl").write_text("y" * 99, encoding="utf-8")  # другой размер
    assert reg_mod.source_version(src) != v1
    _dump(src / "sub", {"b.bsl": 5})  # новый файл
    assert reg_mod.source_version(src) != v1
    assert reg_mod.source_version(homes / "нет-такой") is None


def test_set_indexed_writes_source_version(homes):
    src = homes / "src2"
    _dump(src, {"m.bsl": 7})
    out = reg_mod.add_config("ep", str(src))
    cid = out["id"]
    out["entry"].pop("source_version")  # как до первой сборки
    ws.save_registry({"configs": {cid: out["entry"]}})
    reg_mod.set_indexed(cid, {"methods": 1})
    entry = reg_mod.get_config(cid)
    assert entry["source_version"] == reg_mod.source_version(src)
    assert entry["indexed_at"] is not None


# -------------------------------------------------------------- симлинк ext

def _home_config_with_stack(homes, short="ut"):
    """Дом-конфигурация с Configuration.xml в source/ и расширением в стеке."""
    out = reg_mod.add_config(short)
    cid = out["id"]
    _dump(reg_mod.config_source_root(cid), {"m.bsl": 3})
    reg_mod.stack_add(cid, "Расш1", 1, "Исправление")
    return cid


@needs_symlink
def test_ensure_ext_link_creates_and_idempotent(homes):
    cid = _home_config_with_stack(homes)
    out = reg_mod.ensure_ext_link(cid)
    assert out.get("ok"), out
    link = Path(out["link"])
    assert link.is_symlink()
    assert link.resolve() == (homes / "extensions" / cid).resolve()
    # идемпотентно
    assert reg_mod.ensure_ext_link(cid).get("ok")


@needs_symlink
def test_ensure_ext_link_skips_empty_stack(homes):
    out = reg_mod.add_config("ka2")
    reg_mod.config_source_root(out["id"]).mkdir(parents=True)
    result = reg_mod.ensure_ext_link(out["id"])
    assert "skipped" in result and "пуст" in result["skipped"]
    assert not reg_mod.config_source_root(out["id"]).exists() or not any(
        reg_mod.config_source_root(out["id"]).iterdir())


def test_ensure_ext_link_skips_legacy_paths(homes):
    src = homes.parent / "legacy_dump"
    _dump(src, {"m.bsl": 1})
    out = reg_mod.add_config("ka3", str(src))
    reg_mod.stack_add(out["id"], "Расш1", 1, "Исправление")
    assert "skipped" in reg_mod.ensure_ext_link(out["id"])
    assert not (src / "ext").exists()


@needs_symlink
def test_build_paths_home_config(homes):
    cid = _home_config_with_stack(homes, short="ep2")
    paths = reg_mod.build_paths(cid)
    assert "error" not in paths
    assert Path(paths["source_root"]) == reg_mod.config_source_root(cid)
    assert Path(paths["index_db"]) == reg_mod.config_index_db(cid)
