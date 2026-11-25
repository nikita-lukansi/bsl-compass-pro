# -*- coding: utf-8 -*-
"""Тесты CLI-оркестратора src.manage (S5): add/reindex/extension/status/
migrate на tmp-домах. Сборка gyrfalcon подменяется (build_index_now) —
CLI-логика (копии, реестр, стеки, сводки) проверяется герметично."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import manage  # noqa: E402
from src import registry as reg_mod  # noqa: E402
from src import state  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Хаб дампов + хабы дом-схемы + system-settings в tmp."""
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    monkeypatch.setattr(state, "DUMPS_HUB", dumps)
    monkeypatch.setenv("COMPASS_CONFIGS_ROOT", str(tmp_path / "data" / "configs"))
    monkeypatch.setenv("COMPASS_DOCS_ROOT", str(tmp_path / "data" / "docs"))
    monkeypatch.setenv("COMPASS_EXTENSIONS_ROOT",
                       str(tmp_path / "data" / "extensions"))
    monkeypatch.setenv("COMPASS_SETTINGS_PATH",
                       str(tmp_path / "data" / "system" / "settings"))
    monkeypatch.delenv("COMPASS_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("COMPASS_INDEX_DB", raising=False)
    ws._TEXT_CACHE.clear()
    ws.init_files()
    return tmp_path


@pytest.fixture
def fake_build(monkeypatch):
    """Сборка подменена: создаёт index.db (валидный путь) без gyrfalcon."""
    def _fake(cid: str, paths: dict) -> dict:
        paths["index_db"].parent.mkdir(parents=True, exist_ok=True)
        paths["index_db"].write_bytes(b"sqlite-fake")
        reg_mod.set_indexed(cid)
        return {"built": str(paths["index_db"]), "tail": []}

    monkeypatch.setattr(manage, "build_index_now", _fake)


def _dump(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Configuration.xml").write_text(
        '<c><Properties><Name>УТ</Name><Version>3.1</Version></Properties></c>',
        encoding="utf-8")
    (root / "Модуль.bsl").write_text("Процедура Х() КонецПроцедуры",
                                     encoding="utf-8")
    return root


def _run(argv):
    """CLI без SystemExit, с перехватом stdout (JSON-вывод)."""
    from src.manage import main
    return main(argv)


def _run_out(argv, capsys):
    """_run + сразу съесть вывод (capsys копит между вызовами)."""
    code = _run(argv)
    captured = capsys.readouterr().out
    return code, (json.loads(captured) if captured.strip() else None)


# -------------------------------------------------------------------- add

def test_add_without_source_lists_hub(homes, capsys):
    _dump(homes / "dumps" / "ут_dump")
    assert _run(["add", "--name", "ut"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] and out["available"] == ["ут_dump"]


def test_add_full_flow(homes, fake_build, capsys):
    _dump(homes / "dumps" / "УТ")
    assert _run(["add", "--name", "ut", "--source", "УТ"]) == 0
    out = json.loads(capsys.readouterr().out)
    cid = out["added"]
    # рабочая копия в дом, конфигурация в реестре, index.db собран (фейк)
    src = reg_mod.config_source_root(cid)
    assert (src / "Configuration.xml").exists()
    assert (src / "Модуль.bsl").exists()
    entry = reg_mod.get_config(cid)
    assert entry["source_root"] == str(src)
    assert entry["source_version"] == reg_mod.source_version(src)
    assert entry["indexed_at"] is not None
    assert Path(out["build"]["built"]).exists()


def test_add_rejects_non_dump(homes):
    folder = homes / "пустая"
    folder.mkdir()
    assert _run(["add", "--name", "ut", "--source", str(folder)]) == 1


def test_add_with_extension(homes, fake_build, capsys):
    ext = homes / "dumps" / "Расш1"
    ext.mkdir()
    (ext / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    _dump(homes / "dumps" / "УТ2")
    assert _run(["add", "--name", "ut2", "--source", "УТ2",
                 "--extension", "Расш1:Исправление:2"]) == 0
    out = json.loads(capsys.readouterr().out)
    cid = out["added"]
    ext_home = reg_mod.config_extension_root(cid, "Расш1")
    assert (ext_home / "Configuration.xml").exists()
    stack = reg_mod.stack_of(cid)
    assert stack[0]["name"] == "Расш1"
    assert stack[0]["purpose"] == "Patch"
    assert stack[0]["position"] == 2
    assert out["extensions"][0]["extension"] == "Расш1"


def test_add_bad_extension_format(homes, fake_build):
    _dump(homes / "dumps" / "УТ3")
    # РГХ: 2 части вместо трёх → error, конфигурация не создаётся «втихую»
    assert _run(["add", "--name", "ut3", "--source", "УТ3",
                 "--extension", "Только-Имя"]) == 1


# --------------------------------------------------------- extension sync

def test_extension_sync_reports_missing(homes, fake_build, capsys):
    _dump(homes / "dumps" / "УТ4")
    ext = homes / "dumps" / "Расш1"
    ext.mkdir()
    (ext / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    code, out = _run_out(["add", "--name", "ut4", "--source", "УТ4",
                          "--extension", "Расш1:Дополнение:1"], capsys)
    assert code == 0, out.get("details") or out
    cid = out["added"]
    # папка на диске без записи стека — sync её покажет
    reg_mod.stack_remove(cid, "Расш1")
    assert _run(["extension", "sync", "--config", cid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["missing_in_stack"] == ["Расш1"]
    # и наоборот: стек без каталога
    reg_mod.stack_add(cid, "Расш2", 1, "Дополнение")
    assert _run(["extension", "sync", "--config", cid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["missing_on_disk"] == ["Расш2"]


# ------------------------------------------------------------ reindex/help

def test_reindex_requires_config(homes, monkeypatch):
    """Нет активной конфигурации и не задан --config — честный отказ."""
    monkeypatch.setenv("COMPASS_SETTINGS_PATH",
                       str(homes / "data" / "system" / "settings"))
    assert _run(["reindex"]) == 1


def test_help_sync_requires_index(homes):
    _dump(homes / "dumps" / "УТ5")
    assert _run(["add", "--name", "ut5", "--source", "УТ5",
                 "--no-build"]) == 0
    cid = next(iter(reg_mod.load_registry()["configs"]))
    # индекса нет → отказ ДО сети (креды не нужны)
    assert _run(["help", "sync", "ka25doc", "--config", cid]) == 1


def test_migrate_calls_s4(homes, monkeypatch, capsys):
    """migrate → registry.migrate_to_homes (S4); error в результате →
    exit-код 1 (для агентов)."""
    monkeypatch.setattr("src.registry.migrate_to_homes",
                        lambda cid=None: {"migrated": ["Compass_x"],
                                          "errors": [{"cid": "Compass_y",
                                                      "error": "нет"}]})
    assert _run(["migrate"]) == 1  # есть errors → неуспех
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"] == ["Compass_x"] and len(out["errors"]) == 1


# ----------------------------------------------------------------- status

def test_status_shows_homes(homes, fake_build, capsys):
    _dump(homes / "dumps" / "УТ6")
    code, _ = _run_out(["add", "--name", "ut6", "--source", "УТ6"], capsys)
    assert code == 0
    code, out = _run_out(["status"], capsys)
    assert code == 0
    assert out["system"]["root"].endswith("system")
    assert len(out["configs"]) == 1
    cfg = out["configs"][0]
    assert cfg["home_scheme"] is True
    assert cfg["index_exists"] is True
    assert cfg["source_fresh"] is True  # сборка записала source_version
    assert cfg["docs"] == []
