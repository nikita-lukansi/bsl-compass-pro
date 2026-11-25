# -*- coding: utf-8 -*-
"""compass-zi2 — автопрогрев int8 после рестарта qdrant: форсированный
повторный enable (set_always_ram force — конфиг always_ram персистит
рестарт, introspect лжёт «warm»), триггеры (первый живой контакт стека,
dead→alive в codesearch), анти-шторм (окно SEMANTIC_AUTOWARM_INTERVAL,
векторы>0, SEMANTIC_WARM=off)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import semantic_tools as st  # noqa: E402
from src.facade import tools  # noqa: E402
from src.semantic import vector_store as vs  # noqa: E402


# ------------------------------------------------- set_always_ram(force=True)

class _QuantClient:
    """Коллекция с scalar int8-квантизацией; ловит update_collection."""

    def __init__(self, always_ram=True):
        self.info = SimpleNamespace(config=SimpleNamespace(
            quantization_config=SimpleNamespace(
                scalar=SimpleNamespace(always_ram=always_ram,
                                       quantile=0.99, type="int8"))))
        self.updates: list[dict] = []

    def get_collection(self, name):
        return self.info

    def update_collection(self, **kwargs):
        self.updates.append(kwargs)


def test_set_always_ram_force_reenables_when_already_true():
    """После рестарта qdrant конфиг уже always_ram=True, а RAM-копия
    холодная: force=True шлёт повторный enable (лечение 17.11), а не
    no-op skip; конфиг квантизации воспроизведён."""
    client = _QuantClient(always_ram=True)
    out = vs.set_always_ram(client, "cfg_x_code", True, force=True)
    assert out == {"updated": True, "collection": "cfg_x_code",
                   "forced": True}
    assert len(client.updates) == 1
    scalar = client.updates[0]["quantization_config"].scalar
    assert scalar.always_ram is True
    assert scalar.quantile == vs._QUANT_QUANTILE


def test_set_always_ram_force_skips_without_quantization():
    """force не создаёт квантизацию PATCH'ем — честный skip остаётся."""
    class _NoQuant:
        def get_collection(self, name):
            return SimpleNamespace(
                config=SimpleNamespace(quantization_config=None))

        def update_collection(self, **kwargs):
            raise AssertionError("PATCH не должен выполняться")

    out = vs.set_always_ram(_NoQuant(), "cfg_x_code", True, force=True)
    assert "skipped" in out and "квантизации" in out["skipped"]


def test_set_always_ram_without_force_keeps_noop():
    """Без force прежний контракт (scripts/reindex.py не меняется):
    тот же флаг — no-op skip, PATCH не уходит."""
    client = _QuantClient(always_ram=True)
    out = vs.set_always_ram(client, "cfg_x_code", True)
    assert "skipped" in out and "уже" in out["skipped"]
    assert client.updates == []


# ------------------------------------------------------------ окружение

class _SyncThread:
    """Поток-заглушка: target выполняется синхронно в start() — прогрев
    в тестах детерминирован, гонок нет."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _fake_st_config(cid):
    """st.config с активной конфигурацией фикстуры (semantic_ready,
    qdrant_url пустой — VectorStore всё равно подменён)."""
    return SimpleNamespace(
        semantic_ready=True, qdrant_url="",
        embedding_settings=lambda: {"base_url": "http://x", "api_key": "",
                                    "model": "m", "source": "env"},
        active_config_id=cid)


@pytest.fixture
def autowarm_env(tmp_path, monkeypatch):
    """Реестр в tmp (одна конфигурация, points>0), фейковый VectorStore с
    клиентом-ловушкой update_collection (стартовое ram=True — «рестарт
    уже был»), синхронные потоки и чистое состояние _sem."""
    from src import registry as registry_mod
    from src import workspace as ws

    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    # Б1-причина 2: monotonic свежей ВМ около нуля — «обнуллённая» метка
    # last_warm_run_at НЕ истекает (окно 900 c держит автопрогрев). Время
    # задаём моком monotonic в модуле-потребителе, метку не трогаем.
    monkeypatch.setattr(st.time, "monotonic", lambda: 1_000_000.0)
    ws._TEXT_CACHE.clear()
    cid = registry_mod.add_config("AW", str(tmp_path / "src"),
                                  mode="full")["id"]
    registry_mod.set_vectors(cid, 500, "test-model", 1024)

    updates: list[tuple[str, bool]] = []

    class _WarmClient:
        def __init__(self):
            self.ram = {"cfg_aw_code": True}

        def get_collection(self, name):
            return SimpleNamespace(config=SimpleNamespace(
                quantization_config=SimpleNamespace(scalar=SimpleNamespace(
                    always_ram=self.ram[name], quantile=0.99,
                    type="int8"))))

        def update_collection(self, **kwargs):
            name = kwargs["collection_name"]
            flag = kwargs["quantization_config"].scalar.always_ram
            updates.append((name, flag))
            self.ram[name] = flag

    client = _WarmClient()

    class _Store:
        def __init__(self, **kwargs):
            self.client = client

        def collection(self, name):
            return SimpleNamespace(count=lambda: 500)

    monkeypatch.setattr(vs, "VectorStore", lambda **kwargs: _Store(**kwargs))
    monkeypatch.setattr(registry_mod, "collection_name",
                        lambda c: "cfg_aw_code")
    monkeypatch.setattr(st, "threading",
                        SimpleNamespace(Thread=_SyncThread))
    monkeypatch.setattr(st, "config", _fake_st_config(cid))
    for key, value in (("collection", None), ("store", None),
                       ("embeddings", None), ("warm_state", None),
                       ("warming", False), ("last_warm_run_at", 0.0),
                       ("qdrant_seen_down", False)):
        monkeypatch.setitem(st._sem, key, value)
    yield {"cid": cid, "updates": updates, "client": client}
    ws._TEXT_CACHE.clear()
    st.reset_semantic_stack()


# ------------------------------------------------------- _maybe_autowarm

def test_autowarm_triggers_forced_enable(autowarm_env):
    """Триггер при cold: активная конфигурация с векторами → фоновый
    форсированный повторный enable — PATCH уходит (несмотря на ram=True),
    warm_state warming→warm, флаг warming снят."""
    st._maybe_autowarm("stack_init")
    assert autowarm_env["updates"] == [("cfg_aw_code", True)]
    assert st._sem["warm_state"]["state"] == "warm"
    assert st._sem["warm_state"]["cid"] == autowarm_env["cid"]
    assert st._sem["warming"] is False


def test_autowarm_idempotent_within_window(autowarm_env):
    """Анти-шторм: повторный cold в окне (дефолт 15 мин) — второго
    запуска нет, сколько бы триггеров ни пришло."""
    st._maybe_autowarm("stack_init")
    st._maybe_autowarm("qdrant_back")
    st._maybe_autowarm("stack_init")
    assert autowarm_env["updates"] == [("cfg_aw_code", True)]


def test_autowarm_window_env_override(autowarm_env, monkeypatch):
    """SEMANTIC_AUTOWARM_INTERVAL=0 — окно не держит: повторы разрешены."""
    monkeypatch.setenv("SEMANTIC_AUTOWARM_INTERVAL", "0")
    st._maybe_autowarm("stack_init")
    st._maybe_autowarm("qdrant_back")
    assert len(autowarm_env["updates"]) == 2


def test_autowarm_skips_without_vectors(autowarm_env):
    """Векторов нет (points=0) — прогрева нет, warming даже не стартует."""
    from src import registry as registry_mod
    registry_mod.set_vectors(autowarm_env["cid"], 0, "test-model", 1024)
    st._maybe_autowarm("stack_init")
    assert autowarm_env["updates"] == []
    assert st._sem["warm_state"] is None
    assert st._sem["warming"] is False


def test_autowarm_skips_in_exotic_mode(autowarm_env, monkeypatch):
    """SEMANTIC_WARM=off — все коллекции в RAM, автопрогрев не нужен."""
    monkeypatch.setenv("SEMANTIC_WARM", "off")
    st._maybe_autowarm("stack_init")
    assert autowarm_env["updates"] == []


def test_autowarm_error_is_honest(autowarm_env, monkeypatch):
    """Сбой PATCH → state cold + error, флаг warming снят (не залипает)."""
    def broken_store(**kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(vs, "VectorStore", broken_store)
    st._maybe_autowarm("stack_init")
    assert st._sem["warm_state"]["state"] == "cold"
    assert "error" in st._sem["warm_state"]
    assert st._sem["warming"] is False


def test_autowarm_skips_when_warming_in_progress(autowarm_env, monkeypatch):
    """Идущий прогрев (config_use) не дублируется автопрогревом."""
    monkeypatch.setitem(st._sem, "warming", True)
    st._maybe_autowarm("stack_init")
    assert autowarm_env["updates"] == []
    assert st._sem["warming"] is True  # чужой флаг не тронут


# ------------------------------------------- первый контакт (холодный старт)

def test_first_contact_after_cold_start_triggers_autowarm(autowarm_env,
                                                          monkeypatch):
    """Первое обращение к семантике после старта ядра: мок источника
    warm_state ответил cold (qdrant жив) → автопрогрев в фоне."""
    monkeypatch.setattr(st, "EmbeddingsClient",
                        lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(st, "_peek_warm_state",
                        lambda store: {"state": "cold",
                                       "updated_at": "2026-11-20T00:00:00Z"})
    monkeypatch.setattr(st, "config", _fake_st_config(autowarm_env["cid"]))
    assert st._ensure_stack() is True
    assert autowarm_env["updates"] == [("cfg_aw_code", True)]
    assert st._sem["warm_state"]["state"] == "warm"


def test_init_with_qdrant_down_marks_seen_down(autowarm_env, monkeypatch):
    """qdrant не отвечал при первом init (peek=unknown): автопрогрев не
    идёт, фиксируется seen_down — оживание увидит codesearch."""
    monkeypatch.setattr(st, "EmbeddingsClient",
                        lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(st, "_peek_warm_state",
                        lambda store: {"state": "unknown"})
    monkeypatch.setattr(st, "config", _fake_st_config(autowarm_env["cid"]))
    assert st._ensure_stack() is True
    assert autowarm_env["updates"] == []
    assert st._sem["qdrant_seen_down"] is True


# ------------------------------------------- codesearch: рестарт под ядром

def test_codesearch_down_then_alive_triggers_autowarm(autowarm_env,
                                                      monkeypatch):
    """dead→alive под живым ядром: первый codesearch честно падает и
    метит даун, оживший — запускает автопрогрев; повтор — без шторма."""
    monkeypatch.setattr(tools, "reader", lambda: object())
    monkeypatch.setattr(st, "_ensure_stack", lambda: True)

    class _FlakyCollection:
        """count() падает один раз (рестарт), дальше отвечает 5 точек."""

        def __init__(self):
            self._failed = False

        def count(self):
            if not self._failed:
                self._failed = True
                raise ConnectionError("refused")
            return 5

    monkeypatch.setitem(st._sem, "collection", _FlakyCollection())
    monkeypatch.setitem(st._sem, "embeddings", SimpleNamespace())

    class _HS:
        def __init__(self, collection=None, embeddings=None):
            pass

        def search(self, query, limit=10, query_timeout=None):
            return [{"name": "МетодМой", "full_path": "p",
                     "metadata": {}, "score": 0.5}]

    monkeypatch.setattr(st, "HybridSearch", _HS)

    out1 = st.codesearch("запрос")
    assert isinstance(out1, dict) and "Qdrant не ответил" in out1["error"]
    assert st._sem["qdrant_seen_down"] is True
    assert autowarm_env["updates"] == []  # на дауне не греем

    out2 = st.codesearch("запрос")
    assert isinstance(out2, list) and out2[0]["name"] == "МетодМой"
    assert autowarm_env["updates"] == [("cfg_aw_code", True)]  # оживание
    assert st._sem["qdrant_seen_down"] is False

    st.codesearch("запрос")
    assert len(autowarm_env["updates"]) == 1  # окно: без второго запуска
