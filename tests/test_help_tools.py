# -*- coding: utf-8 -*-
"""Тесты help-тулов: поиск/чтение/mentions на мини-индексе + гварды help_sync."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import help_tools  # noqa: E402


def test_help_search_finds(mini_reader):
    res = help_tools.help_search("корректировка долга")
    assert isinstance(res, list) and res, "страница зачёта аванса должна найтись"
    row = res[0]
    assert row["source_id"] == "ka25doc"
    assert row["path"].endswith("ru.html")
    assert "snippet" in row


def test_help_search_respects_source_id(mini_reader):
    res = help_tools.help_search("докзаказ", source_id="primary")
    assert isinstance(res, list) and res
    assert all(r["source_id"] == "primary" for r in res)


def test_help_search_no_matches_hint(mini_reader):
    res = help_tools.help_search("несуществующийтерминзапроса")
    assert isinstance(res, dict) and res.get("_no_matches") is True
    assert "форм" in res["hint"], "хинт морфологии обязателен"


def test_help_search_bad_syntax_honest(monkeypatch, mini_reader):
    """Невалидный FTS5-запрос → честный _no_matches, не исключение."""
    res = help_tools.help_search('"незакрытая')
    assert isinstance(res, dict) and res.get("_no_matches")


def test_help_read_full_text(mini_reader):
    res = help_tools.help_read("ka25doc", "Introduction/About/Ext/Help/ru.html")
    assert isinstance(res, dict)
    assert res["title"] == "О программе"
    assert "заказа клиента" in res["text"]
    assert res["mentions"] == []  # в тексте нет имён объектов индекса


def test_help_read_mentions_object(mini_reader):
    """Текст упоминает ДокЗаказ — имя объекта мини-индекса → mentions."""
    res = help_tools.help_read("primary", "Documents/ДокЗаказ/Ext/Help/ru.html")
    assert "ДокЗаказ" in res["mentions"]


def test_help_read_missing_page(mini_reader):
    res = help_tools.help_read("ka25doc", "нет/такой/Ext/Help/ru.html")
    assert res.get("_no_matches") is True


def test_help_status_sources(mini_reader):
    st = help_tools.help_status()
    assert st["fetching"] is False
    assert {s["source_id"] for s in st["sources"]} == {"ka25doc", "primary"}
    assert sum(s["pages"] for s in st["sources"]) == 3


def test_help_sync_gate_no_creds(monkeypatch, mini_reader):
    from src import config as cfg
    monkeypatch.setattr(cfg.Config, "its_credentials",
                        lambda self: {"login": "", "password": "", "cookie": "",
                                      "delay": 2.5, "source": "none"})
    res = help_tools.help_sync()
    assert "кредов" in res.get("error", ""), "честный отказ без кредов"
    assert help_tools._docs["fetching"] is False


def test_help_sync_gate_build_running(monkeypatch, mini_reader):
    """kb sync пишет в тот же index.db, что и сборка → гейт."""
    from src import config as cfg, state
    monkeypatch.setattr(cfg.Config, "its_credentials",
                        lambda self: {"login": "x", "password": "y",
                                      "cookie": "", "delay": 2.5,
                                      "source": "env"})
    saved = dict(state.BUILD_STATE)
    monkeypatch.setitem(state.BUILD_STATE, "building", True)
    try:
        res = help_tools.help_sync()
        assert "пересборка" in res.get("error", "")
        assert help_tools._docs["fetching"] is False
    finally:
        state.BUILD_STATE.update(saved)


def test_help_sync_pause_resume(monkeypatch, mini_reader):
    assert "info" in help_tools.help_sync_pause()  # выгрузка не идёт
    help_tools._docs["fetching"] = True
    try:
        assert help_tools.help_sync_pause()["paused"] is True
        assert not help_tools._docs["pause"].is_set()
        assert help_tools.help_sync_resume()["resumed"] is True
        assert help_tools._docs["pause"].is_set()
    finally:
        help_tools._docs["fetching"] = False
        help_tools._docs["pause"].set()


def test_help_remove_no_index(tmp_path):
    """Нет активного индекса → отказ, не падение."""
    from src.facade import tools
    tools.reset_reader()
    res = help_tools.help_remove("ka25doc")
    assert res.get("error")


# ---------------------------------- P2-4 (аудит 4l5): source_id без валидации

def test_help_sync_bad_source_id_refused(monkeypatch):
    """source_id с обходом/слэшами/пробелами — отказ до любых действий
    (crawler и поток синхронизации не запускаются)."""
    def boom(*args, **kwargs):
        raise AssertionError("синхронизация не должна запускаться")
    monkeypatch.setattr(help_tools, "sync_corpus", boom)
    monkeypatch.setattr(help_tools, "run_crawl", boom)
    for bad in ("../x", "a/b", "..", "a\\b", "", "  ", "с пробелом", "s;rm"):
        res = help_tools.help_sync(bad)
        assert "недопустим" in res.get("error", ""), bad
    assert help_tools._docs["fetching"] is False


def test_help_remove_bad_source_id_refused(monkeypatch):
    """help_remove: невалидный source_id — отказ без запуска gyrfalcon."""
    def boom(cmd, **kwargs):
        raise AssertionError("gyrfalcon kb remove не должен запускаться")
    monkeypatch.setattr(help_tools.subprocess, "run", boom)
    for bad in ("../x", "a/b", "..", "a\\b", ""):
        res = help_tools.help_remove(bad)
        assert "недопустим" in res.get("error", ""), bad
