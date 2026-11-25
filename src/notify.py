# -*- coding: utf-8 -*-
"""Telegram-нотификатор (3.7): события сборки/семантики владельцу.

Fire-and-forget в потоке-демоне: сбой отправки не влияет на работу ядра.
Настройка: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; не заданы — выключено.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

_API = "https://api.telegram.org/bot{token}/sendMessage"


def notify_configured() -> bool:
    """Нотификация включена (токен и чат заданы)."""
    return bool(os.getenv("TELEGRAM_BOT_TOKEN")
                and os.getenv("TELEGRAM_CHAT_ID"))


def _send_sync(text: str) -> bool:
    """Один запрос к Telegram API; False при любой неудаче."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    payload = json.dumps({
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "text": text,
        "disable_notification": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        _API.format(token=token), data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify(text: str) -> None:
    """Отправить уведомление без блокировки вызывающего (поток-демон)."""
    if not notify_configured():
        return
    threading.Thread(target=_send_sync, args=(text,),
                     daemon=True, name="tg-notify").start()


def notify_build_result(alias: str, exit_code: int,
                        tail: list[str] | None = None) -> None:
    """Событие «сборка завершена/упала» — главный сигнал владельцу."""
    status = "✅ сборка завершена" if exit_code == 0 else f"⛔ сборка упала (exit {exit_code})"
    lines = [f"🧭 BSL Compass · {alias}", status]
    if tail:
        lines += ["```" + " ".join(tail[-3:])[:300] + "```"]
    notify("\n".join(lines))
