# -*- coding: utf-8 -*-
"""Тесты расписания пересборки: математика should_run + check_once."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scheduler import RebuildScheduler, should_run  # noqa: E402


def test_should_run_math():
    now = 1_000_000.0
    assert should_run(0, now - 9999, now) is False       # выключено
    assert should_run(30, None, now) is False            # сборок не было
    assert should_run(30, now - 31 * 60, now) is True    # срок истёк
    assert should_run(30, now - 29 * 60, now) is False   # ещё не время
    assert should_run(30, now - 30 * 60, now) is True    # ровно интервал


def test_scheduler_check_once_flow():
    """Полный цикл: не пора → пора → callback взводит новое время."""
    clock = {"now": 1000.0}
    last_build = {"ts": 100.0}
    calls = []

    sched = RebuildScheduler(
        build_callback=lambda: (calls.append(1), True)[1],
        get_last_build_ts=lambda: last_build["ts"],
        interval_min_fn=lambda: 10,
        sleep_fn=lambda s: None,
        now_fn=lambda: clock["now"],
    )
    # 9 минут прошло — не пора
    last_build["ts"] = clock["now"] - 9 * 60
    assert sched.check_once() is False and not calls
    # 11 минут — пора, callback отработал
    clock["now"] += 3 * 60
    assert sched.check_once() is True and len(calls) == 1
    assert sched.last_auto_run == clock["now"]
    # сразу снова — не пора (свежая сборка только что)
    assert sched.check_once() is False


def test_scheduler_callback_failure_does_not_crash():
    """Упавший callback не роняет планировщик (возвращает False)."""
    clock = {"now": 5000.0}

    def boom():
        raise RuntimeError("builder gone")

    sched = RebuildScheduler(
        build_callback=boom,
        get_last_build_ts=lambda: 0.0,
        interval_min_fn=lambda: 1,
        sleep_fn=lambda s: None,
        now_fn=lambda: clock["now"],
    )
    try:
        assert sched.check_once() is False
    except RuntimeError:
        raise AssertionError("планировщик не пережил сбой callback")
