# -*- coding: utf-8 -*-
"""P1-1 (ревью GLM-5.4): sync-тулы не блокируют event loop.

fastmcp 2.14.7 зовёт sync-функцию тула прямо в цикле (эксперимент
ревьюера: time.sleep(1.0) в тулле заморозил цикл ~1 с) — sync-ветка
_tool-обёртки стала корутиной, fn зовётся в потоке (asyncio.to_thread).
Регрессия по образцу test_security_wave2 (P2-3): пока «длинный» тул спит,
цикл тикает и второй параллельный вызов успевает ответить.
"""
import asyncio
import sys
import time
from pathlib import Path

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.main as main_module  # noqa: E402
from src.main import _tool  # noqa: E402


def test_sync_tool_does_not_block_loop(monkeypatch):
    """Sync-тул со sleep идёт через to_thread: тикер набирает тики, второй
    параллельный вызов отвечает до завершения первого (при вызове fn прямо
    в loop — wait_for(0.2) сорвался бы на sleep(0.4), тиков не было бы)."""
    def slow_tool() -> str:
        time.sleep(0.4)
        return "slow"

    def fast_tool() -> str:
        return "fast"

    # _tool регистрирует на глобальный mcp — подменяем на тестовый инстанс,
    # реестр тулов живого сервера не трогаем; _tool возвращает FunctionTool,
    # сама обёртка — в .fn
    monkeypatch.setattr(main_module, "mcp", FastMCP(name="loop-test"))
    slow_wrapper = _tool(slow_tool).fn
    fast_wrapper = _tool(fast_tool).fn

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(12):
                await asyncio.sleep(0.05)
                ticks += 1

        ticker_task = asyncio.create_task(ticker())
        slow_task = asyncio.create_task(slow_wrapper())
        fast_task = asyncio.create_task(fast_wrapper())
        # второй параллельный вызов отвечает, пока первый ещё спит в потоке
        fast_result = await asyncio.wait_for(fast_task, timeout=0.2)
        slow_result = await slow_task
        await ticker_task
        return ticks, fast_result, slow_result

    ticks, fast_result, slow_result = asyncio.run(scenario())
    assert fast_result == '"fast"'  # результат — JSON-строка (как в проде)
    assert slow_result == '"slow"'
    # при блокирующем вызове в loop тикер набрал бы ~0-2 тика за sleep(0.4)
    assert ticks >= 6
