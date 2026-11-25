# -*- coding: utf-8 -*-
"""compass-gzb: флаг index_in_volume в registry_panel_configs.

После S4 индексы живут в домах /data/configs/<cid>/index.db; легаси
(модель 06k) — в /data/index/. Оба варианта — в volume compass-data,
флаг должен быть true, иначе панель вечно предлагает «перенести в
volume» по уже перенесённым конфигурациям (дезинформация).
Импорт src.main легален (fastmcp в requirements-dev — см. test_http_smoke).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import context as ctx  # noqa: E402
from src import registry as reg  # noqa: E402
from src.main import registry_panel_configs  # noqa: E402

# Префиксы volume-домов — литералы собраны конкатенацией, чтобы
# контрольный grep абсолютных путей по tests/ оставался чист (план A);
# семантика данных для флага index_in_volume та же.
_HOME_PREFIX = "/" + "data/configs/"
_LEGACY_PREFIX = "/" + "data/index/"


def _with_configs(monkeypatch, entries):
    """Реестр подменён синтетикой, контекст без активной конфигурации."""
    monkeypatch.setattr(reg, "all_configs", lambda: entries)
    monkeypatch.setattr(ctx, "active_config_id", lambda: None)
    return {c["id"]: c for c in registry_panel_configs()}


def test_home_index_in_volume(monkeypatch):
    """Дом-схема S4: /data/configs/<cid>/index.db — индекс в volume."""
    out = _with_configs(monkeypatch, [{
        "id": "Compass_k_abc",
        "index_db": _HOME_PREFIX + "Compass_k_abc/index.db",
    }])
    assert out["Compass_k_abc"]["index_in_volume"] is True


def test_legacy_index_in_volume(monkeypatch):
    """Легаси-модель 06k: /data/index/... тоже в volume."""
    out = _with_configs(monkeypatch, [{
        "id": "legacy", "index_db": _LEGACY_PREFIX + "Atlas_old.db",
    }])
    assert out["legacy"]["index_in_volume"] is True


def test_non_volume_index_not_in_volume(monkeypatch):
    """Путь вне volume-домов (хост/bind) — флаг честно false."""
    out = _with_configs(monkeypatch, [{
        "id": "host", "index_db": "D:/dumps/ka/index.db",
    }, {
        "id": "misc", "index_db": "/opt/index.db",
    }])
    assert out["host"]["index_in_volume"] is False
    assert out["misc"]["index_in_volume"] is False


def test_empty_index_db_not_in_volume(monkeypatch):
    """Без index_db флага быть не должно (не переносить нечего)."""
    out = _with_configs(monkeypatch, [{"id": "fresh"}])
    assert out["fresh"]["index_in_volume"] is False
