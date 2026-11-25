# -*- coding: utf-8 -*-
"""Авторасписание пересборки индекса (F2): COMPASS_REBUILD_SCHEDULE минут.

Ритм «выгрузка обновляется N раз в день» без ручных вызовов: фоновый
поток раз в минуту проверяет, не истёк ли интервал с последней сборки.
Логика вынесена в чистую функцию — тестируется без потоков и времени.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

TICK_SECONDS = 60


def interval_minutes() -> int:
    """Интервал из env (0/пусто = расписание выключено)."""
    try:
        return max(0, int(os.getenv("COMPASS_REBUILD_SCHEDULE", "0")))
    except ValueError:
        return 0


def should_run(interval_min: int, last_run_ts: float | None,
               now_ts: float) -> bool:
    """Пора ли пересобирать: интервал истёк и была хоть одна сборка правил.

    Пока сборок не было вовсе — не стартуем сами (первую делает визард),
    кроме случая явного интервала при готовом индексе.
    """
    if interval_min <= 0:
        return False
    if last_run_ts is None:
        return False
    return (now_ts - last_run_ts) >= interval_min * 60


class RebuildScheduler:
    """Фоновый планировщик: дергает build_callback, когда срок пришёл.

    last_build_ts поставляется извне (замыкание на BUILD_STATE), чтобы
    ручные пересборки тоже сбрасывали таймер расписания.
    """

    def __init__(self, build_callback: Callable[[], bool],
                 get_last_build_ts: Callable[[], float | None],
                 interval_min_fn: Callable[[], int] = interval_minutes,
                 sleep_fn=time.sleep, now_fn=time.time):
        self._build = build_callback
        self._last_ts = get_last_build_ts
        self._interval = interval_min_fn
        self._sleep = sleep_fn
        self._now = now_fn
        self._stop = threading.Event()
        self.last_auto_run: float | None = None
        self.next_check_at: float | None = None

    def check_once(self) -> bool:
        """Одна проверка (отдельно тестируется): запустить ли сборку.

        Эффективное «последней сборки» = max(BUILD_STATE, last_auto_run):
        пока фоновая сборка не завершилась (finished_at не обновился),
        таймер не должен тикать повторно. Сбой callback не роняет цикл.
        """
        interval = self._interval()
        now = self._now()
        self.next_check_at = now + TICK_SECONDS if interval > 0 else None
        effective_last = max(self._last_ts() or 0.0, self.last_auto_run or 0.0)
        if not should_run(interval, effective_last or None, now):
            return False
        try:
            started = bool(self._build())
        except Exception:
            return False  # планировщик переживает разовый сбой билдера
        if started:
            self.last_auto_run = now
        return started

    def run_forever(self) -> None:
        """Цикл потока: проверка раз в TICK_SECONDS до stop()."""
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                pass  # планировщик не должен умирать от разовой ошибки
            self._sleep(TICK_SECONDS)

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True,
                         name="rebuild-scheduler").start()

    def stop(self) -> None:
        self._stop.set()


def last_build_epoch() -> float | None:
    """Время последней завершённой сборки из BUILD_STATE (для таймера)."""
    from .state import BUILD_STATE
    finished = BUILD_STATE.get("finished_at")
    if not finished:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(finished).timestamp()
    except ValueError:
        return None
