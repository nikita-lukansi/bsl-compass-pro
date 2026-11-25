# -*- coding: utf-8 -*-
"""compass-4w7.4 (обратная связь 2026-11-15): версия релиза в config_list.

config_info.Version читается из Configuration.xml при добавлении, но
config_list его не экспортировал — агент не видел версию, на какой
конфигурации работает. + indexed_at и source_version (первые 8 символов
sha256, не полный хэш). Отсутствующее поле — null, не ошибка.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import registry_tools  # noqa: E402
from src.registry import add_config, load_registry, save_registry  # noqa: E402


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Реестр в tmp (паттерн test_registry)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    yield settings_dir
    ws._TEXT_CACHE.clear()


def _tune_entry(cid: str, **fields) -> None:
    """Дописать поля в запись реестра (add_config без Configuration.xml
    даёт пустой config_info — версию и хэш подставляем руками)."""
    reg = load_registry()
    reg["configs"][cid].update(fields)
    save_registry(reg)


def _entry_of(out: list[dict], cid: str) -> dict:
    return next(c for c in out if c["id"] == cid)


def test_version_in_output(registry_env):
    """Запись с config_info.Version → version в выдаче (+indexed_at,
    source_version обрезан до 8 символов)."""
    cid = add_config("ut", "/dumps/ut")["id"]
    _tune_entry(cid,
                config_info={"Name": "УТ", "Version": "11.5.9.18"},
                indexed_at="2026-11-15T10:00:00+00:00",
                source_version="a" * 64)
    row = _entry_of(registry_tools.config_list()["configs"], cid)
    assert row["version"] == "11.5.9.18"
    assert row["indexed_at"] == "2026-11-15T10:00:00+00:00"
    assert row["source_version"] == "a" * 8


def test_missing_fields_are_null(registry_env):
    """Без Configuration.xml (и без сборки) — null, не ошибки и не KeyError."""
    cid = add_config("ka", "/dumps/ka")["id"]
    row = _entry_of(registry_tools.config_list()["configs"], cid)
    assert row["version"] is None
    assert row["indexed_at"] is None
    assert row["source_version"] is None
