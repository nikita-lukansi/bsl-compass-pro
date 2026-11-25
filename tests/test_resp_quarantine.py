# -*- coding: utf-8 -*-
"""Карантин невалидных resp-файлов (compass-u7y, P1-ревью): JSON-массив
и cp1251-кодировка → archive/resp_<id>.broken, тик жив, очередь
продолжает обрабатываться."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime import offline, tasks as tt  # noqa: E402


CALLS = [{"method": "data_query",
          "params": {"query": "ВЫБРАТЬ 1", "limit": 10}}]


def write_valid_resp(root: Path, task_id: str) -> None:
    data = {"version": 1, "task_id": task_id,
            "created_at": "2026-11-11T00:00:00+00:00",
            "base": {"name": "КА", "version": "0.2.0"},
            "results": [{"method": "data_query", "ok": True, "answer": {}}]}
    offline.write_atomic(root / "results" / f"resp_{task_id}.json",
                         json.dumps(data, ensure_ascii=False))


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    r = tt.TaskRegistry(tmp_path)          # watcher НЕ запущен — тики руками
    yield r
    r._stop.set()


def test_array_resp_is_quarantined(reg, tmp_path):
    """JSON-массив верхнего уровня (бил ронял каждый тик с AttributeError):
    файл → карантин, тик не падает, задача живёт."""
    t = reg.submit("chat-1", CALLS)
    offline.write_atomic(tmp_path / "results" / f"resp_{t.id}.json",
                         json.dumps([{"не": "объект"}], ensure_ascii=False))
    reg._tick()                            # до фикса — AttributeError здесь
    assert reg._tasks[t.id].status == "queued"
    assert not (tmp_path / "results" / f"resp_{t.id}.json").exists()
    assert (tmp_path / "archive" / f"resp_{t.id}.broken").exists()


def test_cp1251_resp_is_quarantined(reg, tmp_path):
    """Ответ в cp1251 (UnicodeDecodeError): карантин, тик жив."""
    (tmp_path / "results" / "resp_tforeign1.json").write_bytes(
        "тест".encode("cp1251"))
    reg._tick()
    assert not (tmp_path / "results" / "resp_tforeign1.json").exists()
    assert (tmp_path / "archive" / "resp_tforeign1.broken").exists()


def test_tick_survives_and_queue_keeps_working(reg, tmp_path):
    """Битые файлы не мешают тику: валидный resp в том же проходе
    доходит до ready, очередь продолжает обрабатываться."""
    broken1 = reg.submit("chat-1", CALLS)
    broken2 = reg.submit("chat-2", CALLS)
    good = reg.submit("chat-1", CALLS, "живая задача")
    offline.write_atomic(tmp_path / "results" / f"resp_{broken1.id}.json",
                         json.dumps(["массив"], ensure_ascii=False))
    (tmp_path / "results" / f"resp_{broken2.id}.json").write_bytes(
        "ответ".encode("cp1251"))
    write_valid_resp(tmp_path, good.id)
    reg._tick()                            # один проход — все три файла
    assert (tmp_path / "archive" / f"resp_{broken1.id}.broken").exists()
    assert (tmp_path / "archive" / f"resp_{broken2.id}.broken").exists()
    assert reg._tasks[good.id].status == "ready"
    assert (tmp_path / "archive" / f"resp_{good.id}.loaded").exists()
    assert not list((tmp_path / "results").glob("resp_*.json"))
    # тик жив и после карантина: следующая валидная заявка обрабатывается
    after = reg.submit("chat-1", CALLS, "после карантина")
    write_valid_resp(tmp_path, after.id)
    reg._tick()
    assert reg._tasks[after.id].status == "ready"
