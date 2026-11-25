# -*- coding: utf-8 -*-
"""compass-756: память-гарда полной сборки — оценка свободной памяти
cgroup v2, отказ на крупном исходнике при малом запасе, пропуск на
мелком/без cgroup/первой сборке. Импорт src.main легален (fastmcp в
requirements-dev — см. test_http_smoke).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg  # noqa: E402
from src.main import (  # noqa: E402
    _LARGE_METHODS,
    _memory_gate,
    cgroup_available_bytes,
)


def _cgroup(tmp_path, max_text: str, current: int) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "memory.max").write_text(max_text, encoding="ascii")
    (root / "memory.current").write_text(str(current), encoding="ascii")
    return root


def test_cgroup_available_delta(tmp_path):
    root = _cgroup(tmp_path, "8589934592", 4294967296)  # 8 GiB − 4 GiB
    assert cgroup_available_bytes(str(root)) == 8589934592 - 4294967296


def test_cgroup_unlimited_is_none(tmp_path):
    root = _cgroup(tmp_path, "max", 4294967296)
    assert cgroup_available_bytes(str(root)) is None


def test_cgroup_missing_files_is_none(tmp_path):
    assert cgroup_available_bytes(str(tmp_path / "нет")) is None


def test_cgroup_garbage_is_none(tmp_path):
    root = _cgroup(tmp_path, "not-a-number", 1)
    assert cgroup_available_bytes(str(root)) is None


def test_gate_refuses_large_config_when_tight(monkeypatch):
    """Крупный исходник (methods > 400k) при запасе < 4,5 ГиБ — отказ
    с подсказкой про qdrant."""
    monkeypatch.setattr(reg, "get_config",
                        lambda cid: {"index_counts": {"methods": 653_756}})
    monkeypatch.setattr("src.main.cgroup_available_bytes",
                        lambda *a, **kw: 3 * 1024**3)
    refusal = _memory_gate("Compass_ka_x")
    assert refusal is not None and "qdrant" in refusal


def test_gate_passes_large_config_with_headroom(monkeypatch):
    """Крупный исходник, запас >= 4,5 ГиБ — пускать."""
    monkeypatch.setattr(reg, "get_config",
                        lambda cid: {"index_counts": {"methods": 653_756}})
    monkeypatch.setattr("src.main.cgroup_available_bytes",
                        lambda *a, **kw: 5 * 1024**3)
    assert _memory_gate("Compass_ka_x") is None


def test_gate_passes_small_config_when_tight(monkeypatch):
    """Мелкий исходник — гард не срабатывает даже при None (нет cgroup)."""
    monkeypatch.setattr(reg, "get_config",
                        lambda cid: {"index_counts": {"methods": 100_000}})
    monkeypatch.setattr("src.main.cgroup_available_bytes",
                        lambda *a, **kw: None)
    assert _memory_gate("Compass_ut_x") is None


def test_gate_passes_first_build_without_counts(monkeypatch):
    """Кэша счётчиков нет (первая сборка) — гард не оценивает
    (документированное поведение; правило — README «Обновление»)."""
    monkeypatch.setattr(reg, "get_config", lambda cid: {})
    monkeypatch.setattr("src.main.cgroup_available_bytes",
                        lambda *a, **kw: 0)
    assert _memory_gate("Compass_new_x") is None


def test_gate_threshold_is_conservative():
    """Порог крупности из ТЗ: методы кэша > 400k; запас 4,5 ГиБ."""
    assert _LARGE_METHODS == 400_000


def test_gate_wired_into_start_build_locked():
    """Строковый ассерт в стиле набора: гард вызывается в
    _start_build_locked после проверки путей, до старта потока."""
    text = (Path(__file__).parent.parent / "src" / "main.py").read_text(
        encoding="utf-8")
    start = text.index("def _start_build_locked(")
    end = text.index("\ndef ", start + 1)
    body = text[start:end]
    assert "_memory_gate(cid)" in body
    assert body.index('"error" in paths') < body.index("_memory_gate(cid)") \
        < body.index("threading.Thread")
