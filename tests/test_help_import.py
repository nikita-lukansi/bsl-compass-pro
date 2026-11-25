# -*- coding: utf-8 -*-
"""Приём готового help-корпуса из DOCS_HUB (compass-85s): import_corpus
копирует корпус в дом docs/<cid>/<source_id> и запускает локальный
gyrfalcon kb sync (subprocess-мок) БЕЗ ИТС-кредов; честные отказы
(нет в хабе, пустой, дом занят, сборка/выгрузка идёт, нет индекса)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as registry_mod  # noqa: E402
from src import state  # noqa: E402
from src import workspace as ws  # noqa: E402
from src.facade import help_tools  # noqa: E402
from src.facade import tools as facade_tools  # noqa: E402


@pytest.fixture
def import_env(tmp_path, monkeypatch):
    """Реестр в tmp + docs-дом и DOCS_HUB на tmp + активный индекс-заглушка."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    monkeypatch.setenv("COMPASS_DOCS_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("DOCS_HUB", str(tmp_path / "docs-dumps"))
    ws._TEXT_CACHE.clear()
    cid = registry_mod.add_config("KA", str(tmp_path / "src"))["id"]
    settings = ws.load_settings()
    settings["default_config_id"] = cid
    ws.save_settings(settings)
    db = tmp_path / "index.db"
    monkeypatch.setattr(facade_tools, "active_index_db", lambda: db)
    yield {"cid": cid, "hub": tmp_path / "docs-dumps",
           "docs": tmp_path / "docs", "db": db}
    ws._TEXT_CACHE.clear()


def _hub_corpus(hub: Path, source_id: str = "ka25doc2") -> Path:
    """Готовый корпус в хабе: пара страниц (path/Ext/Help/ru.html)."""
    root = hub / source_id
    page = root / "Введение" / "Опрограмме" / "Ext" / "Help" / "ru.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html>О программе</html>", encoding="utf-8")
    return root


def _fake_kb(returncode=0, tail="синхронизировано"):
    return SimpleNamespace(returncode=returncode, stdout=tail, stderr="")


def test_import_corpus_copies_and_kbsyncs(import_env, monkeypatch):
    cid, hub = import_env["cid"], import_env["hub"]
    _hub_corpus(hub)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_kb()

    monkeypatch.setattr(help_tools.subprocess, "run", fake_run)
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert out["ok"] is True
    dst = import_env["docs"] / cid / "ka25doc2"
    assert (dst / "Введение" / "Опрограмме" / "Ext" / "Help" / "ru.html"
            ).exists()
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["gyrfalcon", "kb", "sync"]
    assert str(import_env["db"]) in cmd
    assert "--source-id" in cmd and "ka25doc2" in cmd
    assert "--src" in cmd and str(dst) in cmd


def test_help_import_wrapper_goes_through(import_env, monkeypatch):
    """Тул help_import — тонкая обёртка import_corpus (тот же путь)."""
    _hub_corpus(import_env["hub"])
    monkeypatch.setattr(help_tools.subprocess, "run",
                        lambda cmd, **kw: _fake_kb())
    out = help_tools.help_import("ka25doc2", cid=import_env["cid"])
    assert out["ok"] is True


def test_import_corpus_kb_failure_is_honest(import_env, monkeypatch):
    """kb sync упал (exit 1) — ok=False, хвост отчёта на месте."""
    _hub_corpus(import_env["hub"])
    monkeypatch.setattr(help_tools.subprocess, "run",
                        lambda cmd, **kw: _fake_kb(returncode=1,
                                                   tail="ошибка разбора"))
    out = help_tools.import_corpus("ka25doc2", cid=import_env["cid"])
    assert out["ok"] is False
    assert out["kb_sync"]["exit_code"] == 1
    assert "ошибка разбора" in " ".join(out["kb_sync"]["tail"])


def test_import_corpus_kb_missing_binary(import_env, monkeypatch):
    """gyrfalcon нет в PATH — честный отказ, копия корпуса сохранена."""
    _hub_corpus(import_env["hub"])

    def boom(cmd, **kwargs):
        raise FileNotFoundError("[WinError 2]")

    monkeypatch.setattr(help_tools.subprocess, "run", boom)
    out = help_tools.import_corpus("ka25doc2", cid=import_env["cid"])
    assert "error" in out and "kb sync" in out["error"]
    dst = import_env["docs"] / import_env["cid"] / "ka25doc2"
    assert dst.is_dir() and any(dst.iterdir())


def test_import_corpus_refusals(import_env, monkeypatch):
    cid = import_env["cid"]
    # source_id небезопасен (становится путём)
    for bad in ("../evil", "a/b", "", "с пробелом"):
        out = help_tools.import_corpus(bad, cid=cid)
        assert "error" in out, bad
    # нет корпуса в хабе
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert "error" in out and "нет в хабе" in out["error"]
    # пустой корпус
    (import_env["hub"] / "пустой").mkdir(parents=True)
    out = help_tools.import_corpus("пустой", cid=cid)
    assert "error" in out and "пуст" in out["error"]
    # дом корпуса занят
    _hub_corpus(import_env["hub"])
    occupied = import_env["docs"] / cid / "ka25doc2"
    occupied.mkdir(parents=True)
    (occupied / "старое.html").write_text("x", encoding="utf-8")
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert "error" in out and "занят" in out["error"]
    assert (occupied / "старое.html").exists()
    (occupied / "старое.html").unlink()
    occupied.rmdir()
    # идёт сборка индекса
    monkeypatch.setattr(state, "BUILD_STATE", {"building": True})
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert "error" in out and "index.db" in out["error"]
    monkeypatch.setattr(state, "BUILD_STATE", {"building": False})
    # идёт выгрузка документации
    monkeypatch.setitem(help_tools._docs, "fetching", True)
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert "error" in out and "синхронизация" in out["error"]
    monkeypatch.setitem(help_tools._docs, "fetching", False)
    # нет индекса
    monkeypatch.setattr(facade_tools, "active_index_db", lambda: None)
    out = help_tools.import_corpus("ka25doc2", cid=cid)
    assert "error" in out and "индекс" in out["error"]
    monkeypatch.setattr(facade_tools, "active_index_db",
                        lambda: import_env["db"])
    # конфигурация не выбрана
    monkeypatch.setattr("src.context.active_config_id", lambda: None)
    out = help_tools.import_corpus("ka25doc2", cid="")
    assert "error" in out and "не выбрана" in out["error"]
