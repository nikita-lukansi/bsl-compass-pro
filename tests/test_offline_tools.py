# -*- coding: utf-8 -*-
"""Тулы offline_submit/wait: деанон params, изоляция чатов, лимиты."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.runtime import offline, tasks as tt  # noqa: E402
from src.facade import runtime_tools as rt  # noqa: E402


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_MODE", "offline")
    monkeypatch.setattr(offline, "OFFLINE_ROOT", tmp_path)
    reg = tt.TaskRegistry(tmp_path)
    monkeypatch.setattr(tt, "_registry", reg)
    return tmp_path, reg


def test_submit_deanonymizes_tokens_in_file(offline_env):
    tmp_path, reg = offline_env
    # чат chat-1 уже знает токен для ГУИДа; тул берёт чат из X-Chat-Name
    from src.context import chat_name_var
    tvar = chat_name_var.set("chat-1")
    try:
        a = tt.get_anonymizer("chat-1")
        token = a.anonymize_value("e1c4a2b3-0000-1111-2222-333344445555", "Документ")
        out = rt.offline_submit(
            [{"method": "document_movements",
              "params": {"type": "Документ", "name": "РеализацияТоваровУслуг",
                         "key": token}}],
            description="движения")
        assert out.get("task_id")
        raw = json.loads((tmp_path / "queue" / f"req_{out['task_id']}.json")
                         .read_text(encoding="utf-8"))
        # в файле — реальный ГУИД, не токен
        assert raw["calls"][0]["params"]["key"] == \
               "e1c4a2b3-0000-1111-2222-333344445555"
        # в памяти — токеновый вид как пришло от агента
        task = reg._tasks[out["task_id"]]
        assert task.calls[0]["params"]["key"] == token
    finally:
        chat_name_var.reset(tvar)


def test_submit_validates_calls(offline_env):
    bad = rt.offline_submit([{"method": ""}])
    assert "error" in bad
    bad2 = rt.offline_submit("не список")
    assert "error" in bad2


def test_submit_online_mode_hint(monkeypatch, offline_env):
    monkeypatch.setenv("RUNTIME_MODE", "online")
    # compass-vlu: «online + RUNTIME_URL не задан» — обе дорожки перекрыты,
    # это ошибка с путями выхода, а не беззаботное info
    monkeypatch.delenv("RUNTIME_URL", raising=False)
    out = rt.offline_submit([{"method": "ping", "params": {}}])
    assert "error" in out and "hint" in out
    assert "offline_submit" in out["hint"]      # путь (2): офлайн-конвейер
    # online + URL задан: рантайм рабочий — остаётся info-подсказка
    monkeypatch.setenv("RUNTIME_URL", "http://1c:8080/sandbox")
    out2 = rt.offline_submit([{"method": "ping", "params": {}}])
    assert "info" in out2


def test_wait_returns_own_task(offline_env):
    tmp_path, reg = offline_env
    out = rt.offline_submit([{"method": "ping", "params": {}}])
    tid = out["task_id"]
    # resp от 1С
    data = {"version": 1, "task_id": tid, "created_at": "x",
            "base": {}, "results": [{"method": "ping", "ok": True,
                                     "answer": {"Версия": "0.2.0"}}]}
    offline.write_atomic(tmp_path / "results" / f"resp_{tid}.json",
                         json.dumps(data, ensure_ascii=False))
    reg._tick()
    res = rt.offline_wait(tid, timeout=3)
    assert res["status"] in ("ready", "delivered")
    assert res["results"][0]["answer"]["Версия"]


def test_runtime_status_offline_has_rls_hint(offline_env):
    """compass-lz7: офлайн-ветка runtime_status несёт RLS-hint — код
    ACCESS_DENIED обычно означает ограничение на уровне записей, а не
    ошибку агента (факты боя: Организации/Контрагенты/Склады)."""
    st = rt.runtime_status()
    assert st["mode"] == "offline"
    assert "ACCESS_DENIED" in st["rls_hint"] and "RLS" in st["rls_hint"]
    assert "Организации" in st["rls_hint"] and "документ" in st["rls_hint"]


def test_offline_task_status_light_view(offline_env):
    """compass-5vq: статус задачи по id без входа в offline_wait —
    лёгкий вид (без results), has_results, отказ для чужого/неизвестного
    id с подсказкой живых задач своего чата."""
    out = rt.offline_submit([{"method": "data_query",
                              "params": {"query": "ВЫБРАТЬ 1"}}], "статус")
    tid = out["task_id"]
    st = rt.offline_task_status(tid)
    assert st["status"] == "queued" and "results" not in st
    assert st["has_results"] is False
    assert st["calls_methods"] == ["data_query"]
    from src.context import chat_name_var
    tvar = chat_name_var.set("chat-other")
    try:
        alien = rt.offline_task_status(tid)
        assert "error" in alien and alien["my_tasks"] == []
    finally:
        chat_name_var.reset(tvar)
    miss = rt.offline_task_status("t_absent1")
    assert "error" in miss and tid in miss["my_tasks"]


def test_runtime_status_offline_version_badge(offline_env):
    """compass-5vq: после обмена runtime_status несёт бейдж версии
    рантайма (base из resp = Пинг.Версия/Синоним); до обмена — None."""
    tmp_path, reg = offline_env
    assert rt.runtime_status()["base"] is None          # обменов не было
    out = rt.offline_submit([{"method": "ping", "params": {}}])
    data = {"version": 1, "task_id": out["task_id"], "created_at": "x",
            "base": {"name": "КА (демо)", "version": "0.2.2"},
            "results": [{"method": "ping", "ok": True, "answer": {}}]}
    offline.write_atomic(tmp_path / "results" / f"resp_{out['task_id']}.json",
                         json.dumps(data, ensure_ascii=False))
    reg._tick()
    st = rt.runtime_status()
    assert st["base"] == {"name": "КА (демо)", "version": "0.2.2"}
