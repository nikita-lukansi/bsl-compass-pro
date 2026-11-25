# -*- coding: utf-8 -*-
"""compass-sm4: режимы семантики fast/full функциональны.

Решение владельца 16.11 (подтверждено техлидом 23.11): full = семантика
включена, fast = выключена — honest-отказ codesearch (инфо с маршрутом
включения, не ошибка) + сгрузка RAM-копии активной коллекции (fa9-механика
set_always_ram(False)); точки в Qdrant не удаляются никогда. Тул
semantic_mode: fast→full без confirm — только флаг в реестре (платная
векторизация молча не запускается), с confirm — запуск (только для
активной конфигурации контекста); full→fast — флаг + сгрузка. Эхо mode:
config_use и бейдж _config_badge (контракт yho).

Этап 1 (без регистрации тулa в main.py — compass-2ao): функции
тестируются напрямую.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.semantic.vector_store as vs  # noqa: E402
from src import workspace as ws  # noqa: E402
from src.facade import registry_tools, semantic_tools, tools  # noqa: E402
from src.registry import add_config, get_config, update_config  # noqa: E402


@pytest.fixture
def mode_env(tmp_path, monkeypatch):
    """Реестр в tmp: активная ut (full) и неактивная off (fast).

    warm_state обнуляем — детерминизм проверки cold после сгрузки."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    # Б1: локальное хранилище Qdrant (тест без мока VectorStore строит его
    # по-настоящему) — в песочницу, не /data/qdrant
    monkeypatch.setenv("QDRANT_STORAGE", str(tmp_path / "qdrant"))
    ws._TEXT_CACHE.clear()
    src_root = tmp_path / "src"
    src_root.mkdir()
    cid_ut = add_config("ut", str(src_root), display_name="УТ",
                        mode="full")["id"]
    cid_off = add_config("off", str(src_root), mode="fast")["id"]
    ws.save_settings({"default_config_id": cid_ut})
    monkeypatch.setitem(semantic_tools._sem, "warm_state", None)
    yield {"ut": cid_ut, "off": cid_off,
           "ut_collection": f"cfg_{cid_ut}_code"}
    ws._TEXT_CACHE.clear()


@pytest.fixture
def evict_mocks(monkeypatch):
    """Моки сгрузки RAM: VectorStore-фейк и записывающий set_always_ram."""
    seen: dict = {}

    def _fake_set_ram(client, collection, flag, *, force=False):
        seen.setdefault("calls", []).append((collection, flag, force))
        return {"updated": flag, "collection": collection}

    class _Store:
        def __init__(self, **kwargs):
            self.client = object()

    monkeypatch.setattr(vs, "VectorStore", _Store)
    monkeypatch.setattr(vs, "set_always_ram", _fake_set_ram)
    return seen


@pytest.fixture
def vectorize_spy(monkeypatch):
    """Шпион запуска векторизации (платный API — зреть каждый вызов)."""
    calls: list[dict] = []

    def _spy():
        calls.append({"launched": True})
        return {"status": "started", "collection": "cfg_test_code"}

    monkeypatch.setattr(semantic_tools, "start_semantic_index", _spy)
    return calls


# ------------------------------------------------------------ валидация

def test_mode_validation(mode_env):
    """Кривой mode и неизвестный cid — честные отказы."""
    out = semantic_tools.semantic_mode(mode_env["ut"], "turbo")
    assert out == {"error": "mode: fast|full"}
    out = semantic_tools.semantic_mode("Compass_No_Such", "full")
    assert "error" in out and "не найдена" in out["error"]
    assert mode_env["ut"] in out["known"]


def test_same_mode_is_noop(mode_env, evict_mocks, vectorize_spy):
    """Повторное переключение в текущий режим — info, без побочных веток."""
    out = semantic_tools.semantic_mode(mode_env["ut"], "full")
    assert out["info"].startswith("конфигурация уже в режиме")
    assert out["config_id"] == mode_env["ut"] and out["mode"] == "full"
    assert not evict_mocks and not vectorize_spy
    assert get_config(mode_env["ut"])["mode"] == "full"


# ------------------------------------------------- fast → full (ветки confirm)

def test_fast_to_full_without_confirm_only_flag(mode_env, vectorize_spy):
    """confirm=False — только флаг в реестре + подсказка; векторизация
    (платная) НЕ запускается молча."""
    out = semantic_tools.semantic_mode(mode_env["off"], "full")
    assert out["previous"] == "fast" and out["mode"] == "full"
    assert "confirm" in out["vectorize"]["skipped"]
    assert not vectorize_spy, "без confirm запуск запрещён"
    assert get_config(mode_env["off"])["mode"] == "full"


def test_fast_to_full_confirm_starts_for_active(mode_env, vectorize_spy):
    """confirm=True для АКТИВНОЙ конфигурации — векторизация запущена,
    результат старта прокинут в ответ."""
    update_config(mode_env["ut"], mode="fast")
    out = semantic_tools.semantic_mode(mode_env["ut"], "full", confirm=True)
    assert len(vectorize_spy) == 1
    assert out["vectorize"] == {"status": "started",
                                "collection": "cfg_test_code"}
    assert get_config(mode_env["ut"])["mode"] == "full"


def test_fast_to_full_confirm_skips_not_active(mode_env, vectorize_spy):
    """confirm=True для НЕактивной — векторизация не запускается
    (start_semantic_index векторизует только активную), честный маршрут."""
    out = semantic_tools.semantic_mode(mode_env["off"], "full", confirm=True)
    assert not vectorize_spy
    assert "не активна" in out["vectorize"]["skipped"]
    assert get_config(mode_env["off"])["mode"] == "full"


# ------------------------------------------------- full → fast (сгрузка RAM)

def test_full_to_fast_evicts_active_ram(mode_env, evict_mocks):
    """Активная конфигурация: флаг + always_ram(False) её коллекции;
    warm_state честно гасится в cold; точки не трогаем (только PATCH)."""
    out = semantic_tools.semantic_mode(mode_env["ut"], "fast")
    assert out["previous"] == "full" and out["mode"] == "fast"
    assert evict_mocks["calls"] == [(mode_env["ut_collection"], False, False)]
    assert out["ram"]["updated"] is False
    assert "сохранены" in out["info"]
    assert get_config(mode_env["ut"])["mode"] == "fast"
    assert semantic_tools._sem["warm_state"]["state"] == "cold"


def test_full_to_fast_inactive_no_evict(mode_env, evict_mocks):
    """Неактивная: RAM-копия не греется — сгрузка не требуется."""
    semantic_tools.semantic_mode(mode_env["off"], "full")
    out = semantic_tools.semantic_mode(mode_env["off"], "fast")
    assert "не активная" in out["ram"]["skipped"]
    assert "calls" not in evict_mocks
    assert get_config(mode_env["off"])["mode"] == "fast"


def test_evict_respects_semantic_warm_off(mode_env, evict_mocks, monkeypatch):
    """SEMANTIC_WARM=off — все коллекции в RAM by design: сгрузки нет."""
    monkeypatch.setenv("SEMANTIC_WARM", "off")
    out = semantic_tools.semantic_mode(mode_env["ut"], "fast")
    assert "SEMANTIC_WARM=off" in out["ram"]["skipped"]
    assert "calls" not in evict_mocks


def test_evict_error_keeps_switch(mode_env, monkeypatch):
    """Сбой сгрузки не роняет переключение: режим сохранён, честная
    пометка в ram.error, верхнеуровневого error нет."""

    def _boom(client, collection, flag, *, force=False):
        raise RuntimeError("qdrant away")

    monkeypatch.setattr(vs, "set_always_ram", _boom)
    out = semantic_tools.semantic_mode(mode_env["ut"], "fast")
    assert "RuntimeError" in out["ram"]["error"]
    assert "error" not in out  # переключение состоялось
    assert get_config(mode_env["ut"])["mode"] == "fast"


# ------------------------------------------------------- codesearch в fast

def test_codesearch_fast_refusal_is_info_before_count(mode_env, evict_mocks,
                                                       monkeypatch):
    """Активная в fast: honest-инфо с маршрутом включения (не ошибка),
    ДО сетевого count(); бейдж config привязывает отказ к конфигурации."""

    class _Boom:
        def count(self):
            raise AssertionError("режимная ветка обязана срабатывать "
                                 "до count() — без сети")

    monkeypatch.setattr(tools, "reader", lambda: object())
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setitem(semantic_tools._sem, "collection", _Boom())
    semantic_tools.semantic_mode(mode_env["ut"], "fast")
    out = semantic_tools.codesearch("скидки")
    assert isinstance(out, dict) and "info" in out and "error" not in out
    assert f"режимом fast конфигурации {mode_env['ut']}" in out["info"]
    assert "semantic_mode" in out["info"] and "full" in out["info"]
    assert out["mode"] == "fast"
    assert out["config"]["id"] == mode_env["ut"]
    assert out["config"]["mode"] == "fast"


def test_codesearch_full_proceeds_to_old_branches(mode_env, monkeypatch):
    """Активная в full: старые ветки живы — пустая коллекция это info
    «векторный индекс пуст» (режимная проверка не перехватывает)."""
    monkeypatch.setattr(tools, "reader", lambda: object())
    monkeypatch.setattr(semantic_tools, "_ensure_stack", lambda: True)
    monkeypatch.setitem(semantic_tools._sem, "collection",
                        type("C", (), {"count": lambda self: 0})())
    out = semantic_tools.codesearch("скидки")
    assert "векторный индекс пуст" in out["info"]


# ------------------------------------------------------------- эхо mode

def test_config_use_echoes_mode(mode_env, monkeypatch):
    """config_use несёт mode выбранной конфигурации (паритет config_list)."""
    monkeypatch.setattr(semantic_tools, "schedule_warmup",
                        lambda prev, new: {"skipped": "test"})
    out = registry_tools.config_use(mode_env["off"])
    assert out["active"] == mode_env["off"]
    assert out["mode"] == "fast"


def test_config_badge_carries_mode(mode_env):
    """Бейдж yho несёт режим: агент видит fast/full в каждом результатном
    ответе (read_function/object_overview/... — тот же _config_badge)."""
    badge = tools._config_badge()
    assert badge["config"]["id"] == mode_env["ut"]
    assert badge["config"]["mode"] == "full"
