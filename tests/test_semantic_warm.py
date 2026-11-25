# -*- coding: utf-8 -*-
"""fa9 — always_ram по активности: новые коллекции без RAM-копии
(SEMANTIC_WARM=off — exotic, всем always_ram), set_always_ram PATCH с
честными skips, порядок прогрева при смене (сначала выгрузка прежней,
потом прогрев новой), warm_state в semantic_status."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import registry_tools  # noqa: E402
from src.facade import semantic_tools as st  # noqa: E402
from src.semantic import vector_store as vs  # noqa: E402


# --------------------------------------------------------------- создание

class _CreatingClient:
    """Ловит create_collection kwargs; коллекции не существуют."""

    def __init__(self):
        self.create_kwargs: list[dict] = []

    def collection_exists(self, name):
        return False

    def create_collection(self, **kwargs):
        self.create_kwargs.append(kwargs)

    def create_payload_index(self, *a, **k):
        pass

    def upsert(self, *a, **k):
        pass


def _make_collection(client):
    return vs.VectorCollection(client, "cfg_test_code", on_disk=True)


def test_new_collection_not_always_ram_by_default(monkeypatch):
    """fa9: новые коллекции on_disk+int8, always_ram=False — RAM не едят."""
    client = _CreatingClient()
    col = _make_collection(client)
    col.upsert(ids=["a"], documents=["т"], metadatas=[{"name": "a"}],
               embeddings=[[1.0, 0.0]])
    quant = client.create_kwargs[0]["quantization_config"]
    assert quant.scalar.always_ram is False
    assert quant.scalar.quantile == vs._QUANT_QUANTILE


def test_semantic_warm_off_returns_always_ram(monkeypatch):
    """SEMANTIC_WARM=off — exotic: всем созданным always_ram=True."""
    monkeypatch.setenv("SEMANTIC_WARM", "off")
    client = _CreatingClient()
    col = _make_collection(client)
    col.upsert(ids=["a"], documents=["т"], metadatas=[{"name": "a"}],
               embeddings=[[1.0, 0.0]])
    assert client.create_kwargs[0]["quantization_config"]\
        .scalar.always_ram is True


# ------------------------------------------------------------ set_always_ram

_NO_QUANT = object()   # sentinel: квантизации нет вовсе


class _QuantClient:
    """Коллекция с scalar-квантизацией; ловит update_collection."""

    def __init__(self, always_ram=True, quant=_NO_QUANT):
        scalar = SimpleNamespace(always_ram=always_ram, quantile=0.99,
                                 type="int8") if quant is _NO_QUANT else None
        self.info = SimpleNamespace(config=SimpleNamespace(
            quantization_config=SimpleNamespace(scalar=scalar)
            if scalar is not None else None))
        self.updates: list[dict] = []

    def get_collection(self, name):
        return self.info

    def update_collection(self, **kwargs):
        self.updates.append(kwargs)


def test_set_always_ram_patches(monkeypatch):
    client = _QuantClient(always_ram=False)
    out = vs.set_always_ram(client, "cfg_x_code", True)
    assert out == {"updated": True, "collection": "cfg_x_code"}
    assert client.updates[0]["quantization_config"]\
        .scalar.always_ram is True
    assert client.updates[0]["quantization_config"]\
        .scalar.quantile == vs._QUANT_QUANTILE   # конфиг воспроизведён


def test_set_always_ram_noop_when_same():
    client = _QuantClient(always_ram=True)
    out = vs.set_always_ram(client, "cfg_x_code", True)
    assert "skipped" in out and client.updates == []


def test_set_always_ram_skips_without_quantization():
    client = _QuantClient(quant=None)
    out = vs.set_always_ram(client, "cfg_x_code", True)
    assert "skipped" in out and "квантизации" in out["skipped"]
    assert client.updates == []


def test_set_always_ram_honest_error_on_unreachable():
    class Down:
        def get_collection(self, name):
            raise ConnectionError("refused")

    out = vs.set_always_ram(Down(), "cfg_x_code", True)
    assert "error" in out and "недоступна" in out["error"]


# --------------------------------------------------------- прогрев при смене

@pytest.fixture
def warm_env(tmp_path, monkeypatch):
    """Реестр в tmp с двумя конфигурациями; VectorStore в _run_warmup
    подменяется на фейковый с клиентом-ловушкой."""
    from src import registry as registry_mod
    from src import workspace as ws

    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    cid_old = registry_mod.add_config("OLD", str(tmp_path / "o"))["id"]
    cid_new = registry_mod.add_config("NEW", str(tmp_path / "n"))["id"]

    calls: list[tuple[str, bool]] = []

    class _WarmClient:
        """Состояние per-collection: прежняя тёплая (была активной),
        новая холодная — как перед реальной сменой конфигурации."""

        def __init__(self):
            self.ram = {"cfg_old_code": True, "cfg_new_code": False}

        def get_collection(self, name):
            return SimpleNamespace(config=SimpleNamespace(
                quantization_config=SimpleNamespace(
                    scalar=SimpleNamespace(always_ram=self.ram[name],
                                           quantile=0.99, type="int8"))))

        def update_collection(self, **kwargs):
            name = kwargs["collection_name"]
            flag = kwargs["quantization_config"].scalar.always_ram
            calls.append((name, flag))
            self.ram[name] = flag

    names = {cid_old: "cfg_old_code", cid_new: "cfg_new_code"}

    class _Store:
        client = _WarmClient()

    def fake_store(**kwargs):
        return _Store()

    monkeypatch.setattr(vs, "VectorStore", fake_store)
    monkeypatch.setattr(registry_mod, "collection_name",
                        lambda cid: names[cid])
    yield {"old": cid_old, "new": cid_new, "calls": calls}
    ws._TEXT_CACHE.clear()
    st.reset_semantic_stack()


def test_warmup_evicts_previous_then_warms_new(warm_env):
    """Порядок при смене: сначала выгрузка прежней (RAM освобождается),
    потом прогрев новой; warm_state warming→warm."""
    out = st._run_warmup(warm_env["old"], warm_env["new"])
    assert warm_env["calls"] == [("cfg_old_code", False),
                                 ("cfg_new_code", True)]
    assert out["state"] == "warm"
    assert st._sem["warm_state"]["state"] == "warm"
    assert st._sem["warm_state"]["cid"] == warm_env["new"]


def test_warmup_without_previous_warms_only(warm_env):
    st._run_warmup(None, warm_env["new"])
    assert warm_env["calls"] == [("cfg_new_code", True)]


def test_warmup_error_is_honest_and_state_cold(warm_env, monkeypatch):
    """Сбой PATCH → state cold + error, не тихий warm."""
    def broken_store(**kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(vs, "VectorStore", broken_store)
    out = st._run_warmup(warm_env["old"], warm_env["new"])
    assert out["state"] == "cold" and "error" in out
    assert st._sem["warm_state"]["state"] == "cold"
    assert "error" in st._sem["warm_state"]


def test_schedule_warmup_skips_in_exotic_mode(monkeypatch):
    monkeypatch.setenv("SEMANTIC_WARM", "off")
    out = st.schedule_warmup(None, "Compass_x")
    assert "skipped" in out


def test_schedule_warmup_no_duplicate_threads(monkeypatch):
    """Идущий прогрев не дублируется: второй вызов — info, не новый поток."""
    monkeypatch.setitem(st._sem, "warming", True)
    out = st.schedule_warmup("a", "b")
    assert "info" in out
    assert st._sem["warming"] is True   # флаг не сброшен


def test_semantic_status_reports_warm_state(monkeypatch):
    monkeypatch.setitem(st._sem, "warm_state",
                        {"cid": "x", "state": "warm"})
    out = st.semantic_status()
    assert out["warm_state"] == "warm"


def test_semantic_status_unknown_before_first_fact(monkeypatch):
    monkeypatch.setitem(st._sem, "warm_state", None)
    out = st.semantic_status()
    assert out["warm_state"] == "unknown"


# -------------------------------------------------------------- config_use

def test_config_use_schedules_warmup(tmp_path, monkeypatch):
    """config_use после бинда вызывает schedule_warmup(previous, new)."""
    from src import registry as registry_mod
    from src import workspace as ws

    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    ws._TEXT_CACHE.clear()
    cid_old = registry_mod.add_config("CU1", str(tmp_path / "a"))["id"]
    cid_new = registry_mod.add_config("CU2", str(tmp_path / "b"))["id"]
    settings = ws.load_settings()
    settings["default_config_id"] = cid_old
    ws.save_settings(settings)

    captured: list[tuple] = []
    import src.facade.semantic_tools as st_mod

    monkeypatch.setattr(st_mod, "schedule_warmup",
                        lambda prev, new: captured.append((prev, new))
                        or {"state": "warming"})
    # первый bind: панель уходит на cid_old (previous=None), второй —
    # переключение old→new с прогревом
    registry_tools.config_use(cid_old)
    out = registry_tools.config_use(cid_new)
    assert out["active"] == cid_new
    assert captured[-1] == (cid_old, cid_new)
    assert out["warm"] == {"state": "warming"}
    ws._TEXT_CACHE.clear()
