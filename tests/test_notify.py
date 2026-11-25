# -*- coding: utf-8 -*-
"""Тесты Telegram-нотификатора: выключен/включён, payload, fire-and-forget."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import notify  # noqa: E402


def test_disabled_when_env_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.notify_configured() is False
    notify.notify("текст")  # тихо, без исключений и потоков


def test_send_payload_and_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C1")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))

        class _R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify._send_sync("привет") is True
    assert captured["url"].endswith("/botT1/sendMessage")
    assert captured["body"]["chat_id"] == "C1"
    assert captured["body"]["text"] == "привет"


def test_send_failure_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")

    def boom(req, timeout=None):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify._send_sync("x") is False  # сбой не поднимает исключение


def test_notify_build_result_async(monkeypatch):
    """notify() не блокирует: отправка уходит в поток-демон."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")
    got: list[str] = []
    done = threading.Event()

    def fake_send(text):
        got.append(text)
        done.set()

    monkeypatch.setattr(notify, "_send_sync", fake_send)
    notify.notify_build_result("ut115", 0, ["итого 497 c"])
    assert done.wait(timeout=2)
    assert "ut115" in got[0] and "сборка завершена" in got[0]
