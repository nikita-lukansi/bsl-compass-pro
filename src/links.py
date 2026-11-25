# -*- coding: utf-8 -*-
"""Ссылки проекта для сайдбара панели: GitHub (+счётчик звёзд) и Telegram.

Счётчик звёзд — прокси к api.github.com с кэшем: панель не упирается в
rate-limit GitHub (60/ч на IP) и не знает слаг. Приватный репозиторий
отдаёт 404 — stars: None (панель прячет цифру, ссылка продолжает
работать). Поведение звезды — как у prism.genlab-1c.ru: клик ведёт на
репозиторий, звёздование за пользователя без OAuth невозможно.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

_CACHE_TTL = 600.0  # сек между обращениями к GitHub API

_lock = threading.Lock()
_cache: dict = {"repo": "", "stars": None, "fetched_at": 0.0}


def github_repo() -> str:
    """Слаг репозитория owner/name (env GITHUB_REPO, дефолт — Pro)."""
    return os.getenv("GITHUB_REPO",
                     "nikita-lukansi/bsl-compass-pro").strip().strip("/")


def telegram_url() -> str:
    """Ссылка на Telegram-профиль автора (env TELEGRAM_URL)."""
    return os.getenv("TELEGRAM_URL", "https://t.me/programmer_nikita").strip()


def _fetch_stars(repo: str, timeout: float = 5.0) -> int | None:
    """stargazers_count репозитория; любая ошибка (404 приватного,
    сеть, лимит) → None — счётчик не критичен для панели."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "bsl-compass-pro"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        count = data.get("stargazers_count")
        return int(count) if count is not None else None
    except Exception:
        return None


def github_info() -> dict:
    """{repo, url, stars} с кэшем на _CACHE_TTL (инвалидация при смене слага).

    Отрицательный результат (None) кэшируется тоже — приватный репозиторий
    не должен дёргать API на каждый рендер панели.
    """
    repo = github_repo()
    now = time.time()
    with _lock:
        if repo == _cache["repo"] and now - _cache["fetched_at"] < _CACHE_TTL:
            return {"repo": repo, "url": f"https://github.com/{repo}",
                    "stars": _cache["stars"]}
    stars = _fetch_stars(repo)
    with _lock:
        _cache.update({"repo": repo, "stars": stars, "fetched_at": now})
    return {"repo": repo, "url": f"https://github.com/{repo}", "stars": stars}


_RELEASE_TTL = 1800.0  # сек между обращениями к /releases/latest
_release_cache: dict = {"repo": "", "data": None, "fetched_at": 0.0}


def latest_release(repo: str | None = None) -> dict | None:
    """Последний релиз GitHub (/releases/latest) с кэшем — бейдж версии
    панели (Волна 1 п.8). Недоступность/404 приватного репо → None
    (молчаливый fallback: бейдж не показывается), как у звёзд."""
    repo = (repo or github_repo()).strip().strip("/")
    now = time.monotonic()
    with _lock:
        if (_release_cache["repo"] == repo
                and now - _release_cache["fetched_at"] < _RELEASE_TTL):
            return _release_cache["data"]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        out = {"tag": data.get("tag_name") or "",
               "url": data.get("html_url") or ""}
    except Exception:
        out = None  # молчаливый fallback — бейдж не показываем
    with _lock:
        _release_cache["repo"] = repo
        _release_cache["data"] = out
        _release_cache["fetched_at"] = now
    return out
