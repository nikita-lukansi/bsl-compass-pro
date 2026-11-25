# -*- coding: utf-8 -*-
"""Проверка актуальности версии: контур (COMPASS_GIT_SHA) vs GitHub main.

compass-82a: агент и панель при входе в контур видят, совпадает ли SHA
собранного образа с HEAD main. Контракт update-блока (зафиксирован вместе
с frontend-агентом compass-4vt):

    {status, current, latest, up_to_date, checked_at, detail?}

    status: "ok" — сравнение выполнено (up_to_date True/False);
            "dev" — ядро вне образа (COMPASS_GIT_SHA=dev), сравнение
                    невозможно;
            "unavailable" — сеть/таймаут/лимит, detail с причиной;
            "private" — 401/404: репо приватное, токена нет/неверен;
            служебные: "off" (UPDATE_CHECK=off), "pending" (первый
            фоновый чек ещё не прошёл; все поля контракта на месте).

Никаких исключений наружу и никаких сетевых вызовов в getStatus-путях:
проверка не должна влиять на работу ядра и не должна блокировать панель.
Свежий запрос (urllib, stdlib) делают только фоновый поток (900 c) и
ручной эндпоинт /api/update-check.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Интервал фонового опроса и задержка первого чека (не мешать загрузке ядра)
CHECK_INTERVAL = 900.0
FIRST_DELAY = 30.0
_TIMEOUT = 5.0

_lock = threading.Lock()
_cache: dict | None = None      # последний результат check_update()
_cache_at: float = 0.0          # time.monotonic() момента записи кэша
_started = False
_started_lock = threading.Lock()

_OFF_VALUES = ("off", "0", "false", "no")


def enabled() -> bool:
    """Проверка включена: env UPDATE_CHECK (дефолт on; off/0/false/no — выкл)."""
    return os.getenv("UPDATE_CHECK", "on").strip().lower() not in _OFF_VALUES


def _now_iso() -> str:
    """Метка времени проверки (UTC, ISO 8601) для checked_at."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _same_commit(current: str, latest: str) -> bool:
    """Один ли коммит: сравнение короткого SHA с полным по префиксу,
    регистронезависимо (нижний регистр HEX у GitHub и у bake)."""
    a, b = current.strip().lower(), latest.strip().lower()
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long.startswith(short)


def _repo_slug() -> str:
    """Слаг репозитория для запроса HEAD main (общий с links.py дефолт)."""
    from . import links
    return links.github_repo()


def _fetch_latest(repo: str, timeout: float = _TIMEOUT) -> tuple[str, str, str]:
    """SHA HEAD main из GitHub API. Возвращает (sha, error_kind, detail).

    error_kind "": успех (sha непустой). "private" — 401/404: репозиторий
    приватный или токен не принят (после публикации репо работает без
    токена). "unavailable" — сеть, таймаут, лимит API, битый ответ.
    Исключений наружу не бросает.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        # GitHub API отклоняет запросы без User-Agent
        "User-Agent": "bsl-compass-pro",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/main", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sha = str(data.get("sha") or "")[:7]
        if sha:
            return sha, "", ""
        return "", "unavailable", "GitHub API: ответ без sha"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 404):
            return "", "private", (
                "репозиторий приватный или токен не принят "
                f"(HTTP {exc.code}); задайте GITHUB_TOKEN в .env "
                "(fine-grained PAT, Contents Read-only); после публикации "
                "репо работает без токена")
        if exc.code == 403:
            return "", "unavailable", (
                "GitHub API отказал (HTTP 403, обычно лимит запросов "
                f"без токена): {exc}")
        return "", "unavailable", f"GitHub API: HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return "", "unavailable", f"сеть недоступна: {exc}"


def check_update() -> dict:
    """Свежая проверка актуальности (сеть) с записью в кэш.

    Возвращает контрактный update-блок; исключений наружу нет. Используется
    фоновым потоком и ручным эндпоинтом /api/update-check (панель/агент).
    """
    from .config import git_sha
    current = git_sha()
    if current == "dev":
        # Вне docker-образа сравнение невозможно в принципе — сеть не трогаем
        out = {"status": "dev", "current": current, "latest": "",
               "up_to_date": None, "checked_at": _now_iso(),
               "detail": "ядро вне образа (COMPASS_GIT_SHA=dev) — сравнение "
                         "с GitHub main невозможно"}
    else:
        sha, kind, detail = _fetch_latest(_repo_slug())
        if not kind:
            out = {"status": "ok", "current": current, "latest": sha,
                   "up_to_date": _same_commit(current, sha),
                   "checked_at": _now_iso()}
        else:
            out = {"status": kind, "current": current, "latest": "",
                   "up_to_date": None, "checked_at": _now_iso(),
                   "detail": detail}
    global _cache, _cache_at
    with _lock:
        _cache = dict(out)
        _cache_at = time.monotonic()
    return dict(out)


def update_status() -> dict:
    """Блок update для /api/status и runtime_status: только кэш, без сети.

    UPDATE_CHECK=off → status "off"; кэш ещё пуст (первый фоновый чек не
    прошёл) → status "pending". Все поля контракта присутствуют всегда.
    """
    from .config import git_sha
    if not enabled():
        return {"status": "off", "current": git_sha(), "latest": "",
                "up_to_date": None, "checked_at": None,
                "detail": "проверка выключена (UPDATE_CHECK=off)"}
    with _lock:
        cached = dict(_cache) if _cache is not None else None
    if cached is not None:
        return cached
    return {"status": "pending", "current": git_sha(), "latest": "",
            "up_to_date": None, "checked_at": None,
            "detail": "первая проверка выполняется в фоне после старта ядра"}


def _tick() -> None:
    """Один цикл фонового опроса: свежий чек, только если кэш устарел
    (CHECK_INTERVAL); в окне кэша сеть не трогаем. Исключения гасятся
    вызывающим циклом — проверка не роняет ядро."""
    global _cache_at
    with _lock:
        fresh = (_cache is not None
                 and time.monotonic() - _cache_at < CHECK_INTERVAL)
    if fresh:
        return
    check_update()


def _loop() -> None:
    """Цикл daemon-потока: задержка FIRST_DELAY (не мешать загрузке ядра),
    затем чек раз в CHECK_INTERVAL."""
    time.sleep(FIRST_DELAY)
    while True:
        try:
            _tick()
        except Exception:
            pass  # фоновая проверка версии не должна ронять ядро
        time.sleep(CHECK_INTERVAL)


def start_background() -> bool:
    """Поднять daemon-поток фонового опроса (идемпотентно).

    Вызов из main() при старте ядра. UPDATE_CHECK=off → False, поток не
    поднимается вовсе (офлайн-контуры без выхода в интернет).
    """
    global _started
    if not enabled():
        return False
    with _started_lock:
        if _started:
            return True
        _started = True
    threading.Thread(target=_loop, daemon=True, name="update-check").start()
    return True
