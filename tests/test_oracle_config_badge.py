# -*- coding: utf-8 -*-
"""compass-4w7.3 (обратная связь 2026-11-15): config-бейдж оракулов +
config_list с активной конфигурацией первой.

Вердикт verify_field/check_claims теперь привязан к конфигурации, чей
индекс проверялся ({config: {id, short_name}}); config_list ставит
активную первой, неактивные — в прежнем порядке (стабильно).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import registry_tools, tools  # noqa: E402
from src.registry import add_config  # noqa: E402


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Реестр в tmp (паттерн test_registry/test_collection_name)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    yield settings_dir
    ws._TEXT_CACHE.clear()


@pytest.fixture
def oracle_env(registry_env, monkeypatch, tmp_path):
    """Мини-индекс + активная конфигурация в реестре — бейдж резолвится."""
    from tests.mini_index import make_mini_db
    cid = add_config("ut", "/dumps/ut", display_name="УТ 11.5")["id"]
    ws.save_settings({"default_config_id": cid})
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    yield {"cid": cid, "short_name": "ut"}
    tools.reset_reader()


def test_verify_field_carries_config(oracle_env):
    """verify_field: каждый ответ несёт config {id, short_name}."""
    out = tools.verify_field("ОбъектТест", "Комментарий")
    assert out["holds"] is True  # вердикт не изменился
    assert out["config"]["id"] == oracle_env["cid"]
    assert out["config"]["short_name"] == "ut"


def test_check_claims_elements_carry_config(oracle_env):
    """check_claims: бейдж в КАЖДОМ элементе — и field (от verify_field),
    и call (мердж в батче; verify_call сам по себе не трогается)."""
    out = tools.check_claims([
        {"kind": "field", "object": "ОбъектТест", "field": "Комментарий"},
        {"kind": "call", "caller": "НетМетода", "callee": "НетТоже"},
    ])
    assert len(out["results"]) == 2
    for elem in out["results"]:
        assert elem["config"]["id"] == oracle_env["cid"]


def test_badge_absent_without_active_config(registry_env, monkeypatch,
                                            tmp_path):
    """Без активной конфигурации поле честно опускается (пустой бейдж
    врал бы о привязке)."""
    from tests.mini_index import make_mini_db
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    try:
        out = tools.verify_field("ОбъектТест", "Комментарий")
        assert "config" not in out
    finally:
        tools.reset_reader()


def test_config_list_active_first(registry_env):
    """config_list: активная первая; после переключения порядок меняется,
    неактивные сохраняют относительный порядок (стабильность)."""
    first = add_config("ut", "/dumps/ut")["id"]
    second = add_config("ka", "/dumps/ka")["id"]
    ws.save_settings({"default_config_id": first})
    out = registry_tools.config_list()
    assert out["configs"][0]["id"] == first and out["configs"][0]["active"]
    # переключение активной → новая первая, прежняя уходит со своим местом
    ws.save_settings({"default_config_id": second})
    out = registry_tools.config_list()
    assert out["configs"][0]["id"] == second and out["configs"][0]["active"]
    assert out["configs"][1]["id"] == first and not out["configs"][1]["active"]
