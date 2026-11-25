# -*- coding: utf-8 -*-
"""Хаб EXT_HUB (compass-85s): extension_add(source="hub") копирует выгрузку
в дом extensions/<cid>/<Имя>/ и регистрирует в стеке; честные отказы
(источника нет, дом занят), предупреждение «не выгрузка», прежний сценарий
без source не сломан."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as registry_mod  # noqa: E402
from src import workspace as ws  # noqa: E402
from src.facade import extension_tools as ext  # noqa: E402


@pytest.fixture
def hub_env(tmp_path, monkeypatch):
    """Реестр в tmp (COMPASS_SETTINGS_PATH) + дома на tmp + хаб EXT_HUB."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_EXTENSIONS_ROOT", str(tmp_path / "exts"))
    monkeypatch.setenv("EXT_HUB", str(tmp_path / "ext-dumps"))
    ws._TEXT_CACHE.clear()
    cid = registry_mod.add_config("KA", str(tmp_path / "src"))["id"]
    settings = ws.load_settings()
    settings["default_config_id"] = cid
    ws.save_settings(settings)
    yield {"cid": cid, "hub": tmp_path / "ext-dumps",
           "exts": tmp_path / "exts"}
    ws._TEXT_CACHE.clear()


def _hub_dump(hub: Path, name: str = "РасшТест") -> Path:
    """Выгрузка расширения в хабе: Configuration.xml + модуль."""
    src = hub / name
    (src / "Forms").mkdir(parents=True)
    (src / "Configuration.xml").write_text("<Configuration/>",
                                           encoding="utf-8")
    (src / "Forms" / "Form.xml").write_text("<Form/>", encoding="utf-8")
    return src


def test_add_from_hub_copies_and_registers(hub_env):
    cid, hub = hub_env["cid"], hub_env["hub"]
    _hub_dump(hub)
    out = ext.extension_add("РасшТест", position=1, purpose="Исправление",
                            source="hub")
    assert out.get("added") == "РасшТест"
    home = hub_env["exts"] / cid / "РасшТест"
    assert (home / "Configuration.xml").exists()
    assert (home / "Forms" / "Form.xml").exists()
    assert [e["name"] for e in registry_mod.stack_of(cid)] == ["РасшТест"]
    assert "warning" not in out


def test_add_from_custom_path(hub_env, tmp_path):
    """source=путь — копия из произвольного каталога (не хаба)."""
    cid = hub_env["cid"]
    elsewhere = tmp_path / "elsewhere" / "РасшПуть"
    (elsewhere / "Configuration.xml").parent.mkdir(parents=True)
    (elsewhere / "Configuration.xml").write_text("<Configuration/>",
                                                 encoding="utf-8")
    out = ext.extension_add("РасшПуть", position=1, purpose="Patch",
                            source=str(elsewhere))
    assert out.get("added") == "РасшПуть"
    assert (hub_env["exts"] / cid / "РасшПуть" / "Configuration.xml").exists()


def test_add_from_hub_missing_source_refused(hub_env):
    cid = hub_env["cid"]
    out = ext.extension_add("НетТакой", position=1, purpose="Patch",
                            source="hub")
    assert "error" in out and "НетТакой" in out["error"]
    assert "EXT_HUB" in out["error"]
    assert registry_mod.stack_of(cid) == []


def test_add_occupied_home_refused(hub_env):
    """Дом расширения уже занят — отказ без перезаписи исходников."""
    cid = hub_env["cid"]
    occupied = hub_env["exts"] / cid / "РасшТест"
    occupied.mkdir(parents=True)
    (occupied / "старый.txt").write_text("не трогать", encoding="utf-8")
    _hub_dump(hub_env["hub"])
    out = ext.extension_add("РасшТест", position=1, purpose="Patch",
                            source="hub")
    assert "error" in out and "занят" in out["error"]
    assert (occupied / "старый.txt").read_text(encoding="utf-8") == "не трогать"


def test_add_non_dump_contents_warned(hub_env):
    """Нет Configuration.xml и *.cfe — копия идёт, но с предупреждением."""
    junk = hub_env["hub"] / "РасшМусор"
    junk.mkdir(parents=True)
    (junk / "readme.txt").write_text("?", encoding="utf-8")
    out = ext.extension_add("РасшМусор", position=1, purpose="Patch",
                            source="hub")
    assert out.get("added") == "РасшМусор"
    assert "не похоже на выгрузку" in out["warning"]


def test_add_without_source_keeps_legacy_path(hub_env):
    """Прежний сценарий: source не задан — регистрация без копирования."""
    cid = hub_env["cid"]
    out = ext.extension_add("РасшСтарый", position=1, purpose="Patch")
    assert out.get("added") == "РасшСтарый"
    assert not (hub_env["exts"] / cid / "РасшСтарый").exists()


# ------------------------------------------- P1-3 (аудит 4l5): traversal-гварды

def test_add_bad_name_refused(hub_env):
    """Имя расширения — разделители путей, '..', пробелы, длиннее 80 —
    честный отказ, стек пуст, ничего не копируется."""
    cid = hub_env["cid"]
    _hub_dump(hub_env["hub"], "evil")
    # пустое имя — отдельная ветка директивы v0.4 (hint), не traversal
    bad_names = ["../evil", "..", "a/b", "a\\b", "sub/../../evil",
                 "Имя с пробелом", "Расш" + "д" * 79, "Точка.xml"]
    for bad in bad_names:
        out = ext.extension_add(bad, position=1, purpose="Patch",
                                source="hub")
        assert "error" in out and "недопустимо" in out["error"], bad
        assert registry_mod.stack_of(cid) == []
        assert not (hub_env["exts"] / cid).exists() or \
            not any((hub_env["exts"] / cid).iterdir()), bad
    # traversal-цель (extensions/<cid>/../evil → extensions/evil) не создана
    assert not (hub_env["exts"] / "evil").exists()


def test_add_valid_name_lands_inside_home(hub_env):
    """Валидное имя (кириллица/латиница/цифры/подчёркивание) — дом строго
    внутри extensions/<cid>/<Имя>/."""
    cid, hub = hub_env["cid"], hub_env["hub"]
    _hub_dump(hub, "Расш_Лат7")
    out = ext.extension_add("Расш_Лат7", position=1, purpose="Addition",
                            source="hub")
    assert out.get("added") == "Расш_Лат7"
    home = (hub_env["exts"] / cid / "Расш_Лат7").resolve()
    home.relative_to(hub_env["exts"].resolve())   # не выше корня расширений
    assert (home / "Configuration.xml").exists()


def test_add_bad_source_refused(hub_env):
    """source-путь: относительный, с '..', несуществующий вне хаба — отказ;
    регистрация не выполняется."""
    cid = hub_env["cid"]
    for bad_src in ("elsewhere/Расш", str(hub_env["hub"] / ".." / "x"),
                    str(hub_env["exts"].parent / "no-such-dump")):
        out = ext.extension_add("РасшТест", position=1, purpose="Patch",
                                source=bad_src)
        assert "error" in out, bad_src
        assert registry_mod.stack_of(cid) == []
    # относительный путь и '..' называются явно
    out = ext.extension_add("РасшТест", position=1, purpose="Patch",
                            source="elsewhere/Расш")
    assert "относительный" in out["error"]
    out = ext.extension_add("РасшТест", position=1, purpose="Patch",
                            source=str(hub_env["hub"] / ".." / "x"))
    assert "'..'" in out["error"]
    out = ext.extension_add("РасшТест", position=1, purpose="Patch",
                            source=str(hub_env["exts"].parent / "no-such"))
    assert "вне хаба" in out["error"]
