# -*- coding: utf-8 -*-
"""Локатор файла панельных HTTP-роутов (compass-2ao, этап миграции).

Структурные аудиты панельных роутов читают файл, где роуты живут сейчас:
после выноса в panel_api.py (этап 2) — src/panel_api.py, до — src/main.py.
Локатор позволяет мигрировать тесты ДО переноса кода и не править их
после: чтение автоматически переключается на новый файл.
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def panel_routes_source() -> Path:
    """Файл с панельными HTTP-роутами: panel_api.py (после выноса) или main.py."""
    panel_api = _SRC / "panel_api.py"
    return panel_api if panel_api.exists() else _SRC / "main.py"
