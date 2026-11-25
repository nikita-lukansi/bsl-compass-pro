# -*- coding: utf-8 -*-
"""Тесты ссылок сайдбара: env/дефолты, кэш счётчика звёзд, фолбэк."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import links  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш звёзд изолирован на каждый тест."""
    links._cache.update({"repo": "", "stars": None, "fetched_at": 0.0})
    yield
    links._cache.update({"repo": "", "stars": None, "fetched_at": 0.0})


def test_defaults(monkeypatch):
    """Без env — слаг Pro-репозитория и Telegram автора."""
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("TELEGRAM_URL", raising=False)
    assert links.github_repo() == "nikita-lukansi/bsl-compass-pro"
    assert links.telegram_url() == "https://t.me/programmer_nikita"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "itnikita00002-rgb/bsl-compass")
    monkeypatch.setenv("TELEGRAM_URL", "https://t.me/other")
    assert links.github_repo() == "itnikita00002-rgb/bsl-compass"
    assert links.telegram_url() == "https://t.me/other"


def test_github_info_cached(monkeypatch):
    """Повторный вызов не дёргает GitHub API (кэш по слагу)."""
    calls = []

    def fake_fetch(repo, timeout=5.0):
        calls.append(repo)
        return 51

    monkeypatch.setattr(links, "_fetch_stars", fake_fetch)
    first = links.github_info()
    assert first["stars"] == 51
    assert first["url"] == "https://github.com/nikita-lukansi/bsl-compass-pro"
    links.github_info()
    links.github_info()
    assert len(calls) == 1  # только первый вызов пошёл в сеть


def test_github_info_repo_change_invalidates(monkeypatch):
    """Смена слага (env) — кэш пересчитывается."""
    calls = []
    monkeypatch.setattr(links, "_fetch_stars",
                        lambda repo, timeout=5.0: calls.append(repo) or 7)
    links.github_info()
    monkeypatch.setenv("GITHUB_REPO", "itnikita00002-rgb/bsl-compass")
    info = links.github_info()
    assert info["repo"] == "itnikita00002-rgb/bsl-compass"
    assert info["stars"] == 7
    assert len(calls) == 2


def test_private_repo_fallback(monkeypatch):
    """404 приватного репо → stars: None (ссылка работает, цифры нет)."""
    monkeypatch.setattr(links, "_fetch_stars", lambda repo, timeout=5.0: None)
    info = links.github_info()
    assert info["stars"] is None
    assert info["url"].endswith("bsl-compass-pro")


def test_fetch_stars_parses_and_falls_back(monkeypatch):
    """_fetch_stars: парсинг stargazers_count; любая ошибка → None."""
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"stargazers_count": 42}).encode("utf-8")

    monkeypatch.setattr(links.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp())
    assert links._fetch_stars("owner/name") == 42

    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(links.urllib.request, "urlopen", boom)
    assert links._fetch_stars("owner/name") is None


def test_latest_release_parses_and_falls_back(monkeypatch):
    """Волна 1 п.8: latest_release — парсинг tag_name; любой сбой → None
    (молчаливый fallback, бейдж не показывается); кэш по слагу."""
    calls = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            calls.append(1)
            return json.dumps({"tag_name": "v1.2.3",
                               "html_url": "https://github.com/o/r/releases/v1.2.3"
                               }).encode("utf-8")

    links._release_cache.update({"repo": "", "data": None, "fetched_at": 0.0})
    monkeypatch.setattr(links.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp())
    out = links.latest_release("owner/name")
    assert out == {"tag": "v1.2.3",
                   "url": "https://github.com/o/r/releases/v1.2.3"}
    links.latest_release("owner/name")
    assert len(calls) == 1  # кэш

    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(links.urllib.request, "urlopen", boom)
    links._release_cache.update({"repo": "", "data": None, "fetched_at": 0.0})
    assert links.latest_release("owner/name") is None
