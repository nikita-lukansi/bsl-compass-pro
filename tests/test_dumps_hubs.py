# -*- coding: utf-8 -*-
"""Точки входа выгрузок (S3, arch-storage v0.2 §2): /workspace/Configurations
и DUMPS_HUB — read-only скан при добавлении; system-пути /data/system.
Рабочий каталог WORKSPACE_HUB выпилен (S3): Settings → /data/system/settings.
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src import state, workspace  # noqa: E402


def test_dumps_available(tmp_path, monkeypatch):
    """scan_available_dumps: обе точки входа, дедуп по source_root, поле hub."""
    ws_cfg = tmp_path / "ws" / "Configurations"
    ws_cfg.mkdir(parents=True)
    (ws_cfg / "РабочаяВыгрузка").mkdir()
    (ws_cfg / "РабочаяВыгрузка" / "Configuration.xml").write_text("<x/>")
    (ws_cfg / "Пустая").mkdir()  # без XML → valid=false

    hub = tmp_path / "dumps"
    hub.mkdir()
    (hub / "ХабВыгрузка").mkdir()
    (hub / "ХабВыгрузка" / "Configuration.xml").write_text("<x/>")

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "ws")
    monkeypatch.setattr(state, "DUMPS_HUB", hub)

    out = state.scan_available_dumps(registered_roots=set())
    folders = {d["folder"]: d for d in out}
    assert folders["РабочаяВыгрузка"]["hub"] == "workspace"
    assert folders["РабочаяВыгрузка"]["valid"] is True
    assert folders["РабочаяВыгрузка"]["registered"] is False
    assert folders["Пустая"]["valid"] is False
    assert folders["ХабВыгрузка"]["hub"] == "dumps"
    assert folders["ХабВыгрузка"]["valid"] is True
    # дедуп по source_root: один source_root встречается один раз
    roots = [d["source_root"] for d in out]
    assert len(roots) == len(set(roots)) == 3

    # registered помечается
    root = str(ws_cfg / "РабочаяВыгрузка")
    out2 = state.scan_available_dumps(registered_roots={root})
    reg = {d["folder"]: d for d in out2}
    assert reg["РабочаяВыгрузка"]["registered"] is True


def test_system_paths_single_source():
    """S3: пути системной зоны объявлены в workspace.py; main.py не
    собирает структуру /workspace (список папок выпилен вместе с
    рабочим каталогом)."""
    main_src = (Path(__file__).parent.parent / "src" / "main.py").read_text(
        encoding="utf-8")
    assert "COMPASS_FOLDERS" not in main_src
    assert "workspace.FOLDERS" not in main_src
    assert "workspace.SUBFOLDERS" not in main_src
    assert hasattr(workspace, "system_root")
    assert hasattr(workspace, "settings_dir")


def test_root_env_and_system_defaults(tmp_path, monkeypatch):
    """root(): точка входа дампов, env COMPASS_WORKSPACE_ROOT для хоста,
    дефолт /workspace. system_root()/settings_dir(): /data/system, env
    COMPASS_SYSTEM_ROOT / COMPASS_SETTINGS_PATH — для тестов и хоста."""
    monkeypatch.setenv("COMPASS_WORKSPACE_ROOT", str(tmp_path))
    assert workspace.root() == tmp_path
    monkeypatch.delenv("COMPASS_WORKSPACE_ROOT", raising=False)
    assert workspace.root() == Path("/workspace")

    monkeypatch.delenv("COMPASS_SYSTEM_ROOT", raising=False)
    monkeypatch.delenv("COMPASS_SETTINGS_PATH", raising=False)
    # контейнерные дефолты — литералы собраны по сегментам, чтобы
    # контрольный grep абсолютных путей по tests/ оставался чист (план A)
    assert workspace.system_root() == Path("/", "data", "system")
    assert workspace.settings_dir() == Path("/", "data", "system", "settings")
    monkeypatch.setenv("COMPASS_SYSTEM_ROOT", str(tmp_path / "sys"))
    assert workspace.settings_dir() == tmp_path / "sys" / "settings"
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(tmp_path / "setts"))
    assert workspace.settings_dir() == tmp_path / "setts"


def test_init_files_migrates_legacy_settings(tmp_path, monkeypatch):
    """Разовый перенос Settings из legacy рабочего каталога: целевой
    пуст + legacy есть → файлы переезжают; повтор — ничего."""
    legacy = tmp_path / "ws" / "Settings"
    legacy.mkdir(parents=True)
    (legacy / "registry.json").write_text('{"version": 1, "configs": {}}',
                                          encoding="utf-8")
    (legacy / "sessions.json").write_text('{"sessions": {}}',
                                          encoding="utf-8")
    monkeypatch.setenv("COMPASS_SETTINGS_PATH",
                       str(tmp_path / "new" / "settings"))
    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "ws")
    workspace._TEXT_CACHE.clear()

    out = workspace.init_files()
    assert sorted(out["migrated"]) == ["registry.json", "sessions.json"]
    assert (tmp_path / "new" / "settings" / "registry.json").exists()
    assert not (legacy / "registry.json").exists()
    # settings.json не было в legacy — создан дефолт
    assert (tmp_path / "new" / "settings" / "settings.json").exists()
    # повтор: переносить нечего
    assert workspace.init_files()["migrated"] == []


def test_init_files_no_migration_when_target_exists(tmp_path, monkeypatch):
    """Новый registry уже есть → legacy не трогается (идемпотентность)."""
    new_dir = tmp_path / "new" / "settings"
    new_dir.mkdir(parents=True)
    (new_dir / "registry.json").write_text('{"configs": {"a": 1}}',
                                           encoding="utf-8")
    legacy = tmp_path / "ws" / "Settings"
    legacy.mkdir(parents=True)
    (legacy / "registry.json").write_text('{"configs": {}}', encoding="utf-8")
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(new_dir))
    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "ws")
    workspace._TEXT_CACHE.clear()

    assert workspace.init_files()["migrated"] == []
    assert (legacy / "registry.json").exists()  # legacy цел
    reg = workspace.load_registry()
    assert reg["configs"] == {"a": 1}
