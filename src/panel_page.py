# -*- coding: utf-8 -*-
"""Fallback /ui: заглушка на случай несобранной панели (R5/A6/P14).

Настоящая панель — Vue в panel/dist (сборка npm; в Docker — node-стейдж
Dockerfile). /ui отдаёт эту заглушку, только если dist не смонтирован.
"""
from __future__ import annotations

PANEL_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BSL Compass Pro — панель не собрана</title>
<style>body{font-family:system-ui,sans-serif;background:#0b0e14;color:#e6edf3;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
div{max-width:640px;padding:24px;border:1px solid #262d3a;border-radius:12px}
code{color:#4c8dff;background:#1b2230;padding:2px 6px;border-radius:6px}</style>
</head><body><div>
<h2>Панель не собрана</h2>
<p>Выполните: <code>cd panel && npm ci && npm run build</code> — затем
пересоберите образ (или запустите node-стейдж Dockerfile).</p>
</div></body></html>
"""
