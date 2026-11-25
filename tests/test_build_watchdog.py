# -*- coding: utf-8 -*-
"""Тесты watchdog зависшей сборки и развязки локов (fc9 + 2xo).

Инцидент fc9: building=True 1.5 ч при завершённом gyrfalcon — save_state
держал _BUILD_LOCK на drvfs-IO, build_finish ждал вечно. Защита:
(а) _STATE_LOCK для файлового состояния, (б) watchdog по PID процесса.
Инцидент 96j (compass-2xo): воркер вышел сам, пост-шаги _run_build
(fill_bodies ~11 мин) держали building=True — watchdog записал ложный
exit=-1 «не жив»; фаза "post" отличает самозавершение от смерти.
"""
import ast
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import state  # noqa: E402

_SRC_MAIN = Path(__file__).parent.parent / "src" / "main.py"


class FakeProc:
    """Двойник Popen: PID + управляемый poll() (None = жив)."""

    def __init__(self, pid: int = 4242, poll_result=None):
        self.pid = pid
        self._poll = poll_result

    def poll(self):
        return self._poll


@pytest.fixture
def fresh_build(monkeypatch):
    """Чистое BUILD_STATE без процесса билдера (изоляция от других тестов)."""
    monkeypatch.setattr(state, "BUILD_STATE", {
        "building": False, "started_at": None, "started_epoch": None,
        "finished_at": None, "exit_code": None, "pid": None, "gen": 0,
        "tail": [], "phase": None, "worker_exit": None,
    })
    monkeypatch.setattr(state, "_BUILD_PROC", None)


# ------------------------------------------------------- слой (а): локи

def test_save_state_does_not_block_build_finish(tmp_path, monkeypatch,
                                                fresh_build):
    """Медленная запись состояния (drvfs) не задерживает build_finish.

    На старом коде оба брали _BUILD_LOCK — build_finish ждал бы IO.
    """
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "config.json")
    release = threading.Event()
    entered = threading.Event()
    orig_write = Path.write_text

    def slow_write_text(self, *args, **kwargs):
        entered.set()
        release.wait(5)
        return orig_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", slow_write_text)

    writer = threading.Thread(
        target=state.save_state, args=({"alias": "ut"},), daemon=True)
    writer.start()
    assert entered.wait(5), "save_state не дошёл до записи"

    t0 = time.time()
    assert state.build_finish(0, ["ok"]) is True
    elapsed = time.time() - t0
    assert not release.is_set(), "запись ещё идёт — финиш не должен был ждать"
    assert elapsed < 1.0, f"build_finish ждал {elapsed:.2f} с — лок общий"

    release.set()
    writer.join(5)


# --------------------------------------------------- слой (б): watchdog

def test_watchdog_needed_matrix():
    now = 1_000_000.0
    old = now - 20 * 60          # 20 мин назад — больше дефолтных 15
    young = now - 5 * 60         # 5 мин назад — меньше порога
    # не building — не трогаем
    assert state.watchdog_needed(False, old, False, now, 15) is False
    # сборка свежая — рано
    assert state.watchdog_needed(True, young, False, now, 15) is False
    # процесс жив — не стреляем
    assert state.watchdog_needed(True, old, True, now, 15) is False
    # процесс не отслежен — вслепую не убиваем
    assert state.watchdog_needed(True, old, None, now, 15) is False
    # зависшая: building + мёртвый процесс + старше порога
    assert state.watchdog_needed(True, old, False, now, 15) is True
    # ровно порог — срабатывает
    assert state.watchdog_needed(True, now - 15 * 60, False, now, 15) is True
    # минутный порог уважается
    assert state.watchdog_needed(True, now - 2 * 60, False, now, 5) is False
    # 2xo: фаза "post" — воркер вышел сам, идут пост-шаги: не смерть,
    # exit=-1 не пишем даже при мёртвом процессе и возрасте за порогом
    assert state.watchdog_needed(True, old, False, now, 15, "post") is False


def test_watchdog_minutes_default_and_env(monkeypatch):
    monkeypatch.delenv("BUILD_WATCHDOG_MIN", raising=False)
    assert state.watchdog_minutes() == 15
    monkeypatch.setenv("BUILD_WATCHDOG_MIN", "7")
    assert state.watchdog_minutes() == 7
    monkeypatch.setenv("BUILD_WATCHDOG_MIN", "мусор")
    assert state.watchdog_minutes() == 15
    monkeypatch.setenv("BUILD_WATCHDOG_MIN", "0")
    assert state.watchdog_minutes() == 1  # минимум 1


def test_build_begin_finish_gen_and_pid(fresh_build):
    gen = state.build_begin()
    assert gen == 1
    assert state.BUILD_STATE["building"] is True
    assert state.BUILD_STATE["started_epoch"] is not None

    state.set_build_proc(FakeProc(pid=999))
    assert state.BUILD_STATE["pid"] == 999
    assert state.build_proc_alive() is True  # poll() → None = ещё жив

    # чужой (просроченный) gen — состояние не трогает
    assert state.build_finish(0, ["поздний"], gen + 5) is False
    assert state.BUILD_STATE["building"] is True

    assert state.build_finish(0, ["готово"], gen) is True
    assert state.BUILD_STATE["building"] is False
    assert state.BUILD_STATE["pid"] is None
    assert state.build_proc_alive() is None  # процесс забыт


def test_watchdog_tick_finishes_stale_dead_build(fresh_build, capsys):
    gen = state.build_begin()
    state.set_build_proc(FakeProc(pid=777, poll_result=1))  # мёртв
    state.BUILD_STATE["started_epoch"] = time.time() - 20 * 60

    report = state.build_watchdog_tick()

    assert report["finished"] is True
    assert state.BUILD_STATE["building"] is False
    assert state.BUILD_STATE["exit_code"] == -1
    assert "watchdog" in state.BUILD_STATE["tail"][0]
    assert "[watchdog]" in capsys.readouterr().out
    # началась новая сборка (gen вырос) — просроченный поток старой
    # сборки, очнувшись после watchdog-сброса, состояние не перезапишет
    assert state.build_begin() == gen + 1
    assert state.build_finish(0, ["очнулся"], gen) is False
    # новая сборка продолжается: не building=False, не чужой tail
    assert state.BUILD_STATE["building"] is True
    assert state.BUILD_STATE["tail"] == []


def test_watchdog_tick_ignores_live_and_young(fresh_build):
    # живой процесс — не трогает
    state.build_begin()
    state.set_build_proc(FakeProc(pid=1, poll_result=None))
    state.BUILD_STATE["started_epoch"] = time.time() - 60 * 60
    assert state.build_watchdog_tick()["finished"] is False
    assert state.BUILD_STATE["building"] is True

    # мёртвый, но моложе порога — не трогает
    state.build_begin()
    state.set_build_proc(FakeProc(pid=2, poll_result=0))
    state.BUILD_STATE["started_epoch"] = time.time() - 2 * 60
    assert state.build_watchdog_tick()["finished"] is False
    assert state.BUILD_STATE["building"] is True

    # не building — вообще ничего
    state.build_finish(0, [])
    assert state.build_watchdog_tick()["finished"] is False


def test_watchdog_tick_survives_internal_error(monkeypatch, fresh_build):
    state.build_begin()
    monkeypatch.setattr(state, "build_proc_alive",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    report = state.build_watchdog_tick()
    assert report["checked"] is False
    assert "RuntimeError" in report["error"]


# --------------------------------------------- 2xo: выход воркера vs смерть

def test_build_phase_post_marks_and_resets(fresh_build):
    """build_phase_post: помечает фазу/код воркера только при живой сборке;
    build_finish снимает фазу (в build-блоке остаётся итог, не процесс)."""
    state.build_phase_post(0)  # сборка не идёт — страховочный no-op
    assert state.BUILD_STATE["phase"] is None

    gen = state.build_begin()
    assert state.BUILD_STATE["phase"] == "build"
    state.build_phase_post(0)
    assert state.BUILD_STATE["phase"] == "post"
    assert state.BUILD_STATE["worker_exit"] == 0
    assert "EXIT=0" in state.BUILD_STATE["tail"][0]  # строка для наблюдателя

    state.build_finish(0, ["готово"], gen)
    assert state.BUILD_STATE["phase"] is None
    assert state.BUILD_STATE["worker_exit"] is None


def test_watchdog_tick_premature_death_still_minus_one(fresh_build):
    """(а) 2xo: воркер умер преждевременно — фаза ещё "build" (поток не
    видел завершения) → прежнее поведение: принудительный finish с -1."""
    state.build_begin()
    assert state.BUILD_STATE["phase"] == "build"
    state.set_build_proc(FakeProc(pid=777, poll_result=-9))  # убит
    state.BUILD_STATE["started_epoch"] = time.time() - 20 * 60

    report = state.build_watchdog_tick()

    assert report["finished"] is True
    assert state.BUILD_STATE["building"] is False
    assert state.BUILD_STATE["exit_code"] == -1
    assert "не жив" in state.BUILD_STATE["tail"][0]


def test_watchdog_tick_post_phase_no_false_minus_one(fresh_build):
    """(б) 2xo (инцидент 96j): воркер вышел сам (EXIT=0), пост-шаги идут
    дольше порога — watchdog НЕ пишет -1; фаза "post" видна наблюдателю."""
    state.build_begin()
    state.set_build_proc(FakeProc(pid=165, poll_result=0))  # вышел сам
    state.build_phase_post(0)
    state.BUILD_STATE["started_epoch"] = time.time() - 20 * 60  # за порогом

    report = state.build_watchdog_tick()

    assert report["finished"] is False
    assert "пост-шаги" in report["reason"]
    assert state.BUILD_STATE["building"] is True   # сборка не сбита
    assert state.BUILD_STATE["exit_code"] is None  # ложный −1 не появился
    assert state.BUILD_STATE["phase"] == "post"    # фаза видна в /api/status
    assert state.BUILD_STATE["worker_exit"] == 0
    # пост-шаги КА длинные (fill_bodies ~11 мин): повторные тики не стреляют
    state.BUILD_STATE["started_epoch"] = time.time() - 40 * 60
    assert state.build_watchdog_tick()["finished"] is False
    assert state.BUILD_STATE["exit_code"] is None


def test_full_success_single_final_no_double_finish(fresh_build):
    """(в) 2xo: полный успех — после пост-шагов один build_finish(0);
    exit_code=0 пишется один раз, тики после финала состояние не трогают."""
    gen = state.build_begin()
    state.set_build_proc(FakeProc(pid=165, poll_result=0))
    state.build_phase_post(0)

    assert state.build_finish(0, ["статистика фаз"], gen) is True
    assert state.BUILD_STATE["exit_code"] == 0
    assert state.BUILD_STATE["building"] is False
    assert state.BUILD_STATE["phase"] is None  # финал снял фазу

    # watchdog после финала не дописывает ни −1, ни второй финал
    finished_at = state.BUILD_STATE["finished_at"]
    assert state.build_watchdog_tick()["finished"] is False
    assert state.BUILD_STATE["exit_code"] == 0
    assert state.BUILD_STATE["finished_at"] == finished_at


def test_run_build_marks_post_phase_after_worker_exit():
    """(2xo) Проводка: _run_build помечает фазу "post" сразу после выхода
    воркера (после communicate, до notify/пост-шагов). main.py не
    импортируется (fastmcp/uvicorn) — AST-верификация по образцу R3."""
    tree = ast.parse(_SRC_MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_run_build")
    src = ast.unparse(fn)
    assert "state.build_phase_post(exit_code)" in src
    # после обоих исходов communicate (обычный и таймаут-kill)
    assert src.index("proc.communicate") < src.index("build_phase_post")
    # до пост-шагов и уведомления — фаза видна, пока они идут
    assert src.index("build_phase_post") < src.index("notify_build_result")
    assert src.index("build_phase_post") < src.index("fill_bodies")
