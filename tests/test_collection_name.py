# -*- coding: utf-8 -*-
"""R2/A1: иммутабельное имя Qdrant-коллекции — ленивая фиксация в реестре.

Кейсы: кандидат cfg_<cid>_code фиксируется; легаси cfg_<display>_code
фиксируется (точки КА остаются под старым именем); зафиксированное имя
не меняется при переименовании entry. Мок store — collection(name).count();
реестр в tmp; без сети.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import semantic_tools  # noqa: E402
from src import registry as reg  # noqa: E402


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Реестр в tmp (паттерн test_registry)."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    return tmp_path


class _MockCollection:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class _MockStore:
    """Мок VectorStore: счётчики точек по именам коллекций."""

    def __init__(self, counts):
        self.counts = counts

    def collection(self, name):
        return _MockCollection(self.counts.get(name, 0))


class _Cfg:
    """Подмена config в semantic_tools: активная конфигурация по cid."""

    def __init__(self, cid, alias="Компас"):
        self._cid = cid
        self._alias = alias

    @property
    def active_config_id(self):
        return self._cid

    @property
    def alias(self):
        return self._alias


def _add_config(display="КА 2.5"):
    return reg.add_config("ka", "/dumps/ka", display_name=display)["id"]


def test_candidate_fixed(workspace_dir, monkeypatch):
    """Живой кандидат cfg_<cid>_code (count>0) → фиксируется в реестр."""
    cid = _add_config()
    monkeypatch.setattr(semantic_tools, "config", _Cfg(cid))
    semantic_tools._fix_collection_name(_MockStore({f"cfg_{cid}_code": 591}))
    assert reg.get_config(cid)["vectors"]["collection_name"] == \
        f"cfg_{cid}_code"
    assert semantic_tools._collection_name(cid) == f"cfg_{cid}_code"


def test_legacy_name_fixed(workspace_dir, monkeypatch):
    """Кандидат пуст, легаси cfg_<display>_code жив → фиксируется легаси
    (точки остаются под старым именем, имя переживает переименования)."""
    cid = _add_config(display="КА 2.5")
    monkeypatch.setattr(semantic_tools, "config", _Cfg(cid))
    semantic_tools._fix_collection_name(
        _MockStore({"cfg_КА 2.5_code": 591}))
    assert reg.get_config(cid)["vectors"]["collection_name"] == \
        "cfg_КА 2.5_code"
    assert semantic_tools._collection_name(cid) == "cfg_КА 2.5_code"


def test_future_candidate_fixed_when_empty(workspace_dir, monkeypatch):
    """Ни кандидат, ни легаси не живы → фиксируется кандидат
    (будущая коллекция, шаг 5 плана R2)."""
    cid = _add_config()
    monkeypatch.setattr(semantic_tools, "config", _Cfg(cid))
    semantic_tools._fix_collection_name(_MockStore({}))
    assert reg.get_config(cid)["vectors"]["collection_name"] == \
        f"cfg_{cid}_code"


def test_fixed_survives_rename(workspace_dir, monkeypatch):
    """Зафиксированное имя не меняется при переименовании entry и при
    повторной фиксации (store жив под кандидатом — не перебивает)."""
    cid = _add_config(display="КА 2.5")
    reg.set_collection_name(cid, "cfg_легаси_code")
    reg.update_config(cid, display_name="Новое Имя")
    monkeypatch.setattr(semantic_tools, "config", _Cfg(cid))
    # store, в котором живой кандидат — попытка перефиксации запрещена
    semantic_tools._fix_collection_name(
        _MockStore({f"cfg_{cid}_code": 591}))
    assert semantic_tools._collection_name(cid) == "cfg_легаси_code"
    assert reg.get_config(cid)["vectors"]["collection_name"] == \
        "cfg_легаси_code"


def test_no_cid_legacy_fallback(workspace_dir, monkeypatch):
    """Без активной конфигурации — легаси-имя по алиасу. С compass-c6z
    (§7 №6) логика в registry.collection_name: подмена semantic_tools.config
    больше не участвует в пути — alias фактический (env COMPASS_ALIAS →
    state → main), делегация проверена целиком."""
    from src import state
    monkeypatch.delenv("COMPASS_ALIAS", raising=False)
    assert semantic_tools._collection_name() == f"cfg_{state.active_alias()}_code"


# ------------------- публичная registry.collection_name (§7 №6, compass-c6z)

_ROOT = Path(__file__).parent.parent


def test_public_name_fixed(workspace_dir):
    """Публичная точка: fixed в реестре → fixed (тот же источник данных,
    что читала приватная _collection_name)."""
    cid = _add_config()
    reg.set_collection_name(cid, "cfg_легаси_code")
    assert reg.collection_name(cid) == "cfg_легаси_code"


def test_public_name_candidate(workspace_dir):
    """Публичная точка: нет fixed → кандидат cfg_<cid>_code."""
    cid = _add_config()
    assert reg.collection_name(cid) == f"cfg_{cid}_code"


def test_public_name_unknown_cid_legacy(workspace_dir, monkeypatch):
    """cid задан, записи в реестре нет → легаси-ветка через alias
    (семантика оригинала сохранена: entry-None не даёт cfg_<cid>_code)."""
    from src.config import config as real_config
    monkeypatch.setattr(type(real_config), "active_config_id",
                        property(lambda self: "Compass_XX_00000000"))
    monkeypatch.setattr(type(real_config), "alias",
                        property(lambda self: "Компас"))
    assert reg.collection_name("Compass_XX_00000000") == "cfg_Компас_code"


def test_public_name_no_active_legacy(workspace_dir, monkeypatch):
    """Без cid и без активной — легаси cfg_<alias>_code; alias — тот же
    путь, что у прежнего _collection_name (env COMPASS_ALIAS → state → main)."""
    from src import state
    monkeypatch.delenv("COMPASS_ALIAS", raising=False)
    assert reg.collection_name() == f"cfg_{state.active_alias()}_code"


def test_backup_uses_public_name():
    """Строковый гейт §7 №6: src/backup.py больше не тянет приватное имя
    слоя семантики — только публичный registry.collection_name."""
    text = (_ROOT / "src" / "backup.py").read_text(encoding="utf-8")
    assert "_collection_name" not in text
