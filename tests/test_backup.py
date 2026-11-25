# -*- coding: utf-8 -*-
"""Тесты движка бэкапов: слепок, ротация, inbox-приём, restore, гварды."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import backup as bk  # noqa: E402
from src import registry as reg  # noqa: E402
from src import state  # noqa: E402


@pytest.fixture
def hubs(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    inbox = tmp_path / "inbox"
    backups.mkdir()
    inbox.mkdir()
    monkeypatch.setattr(bk, "BACKUPS_HUB", backups)
    monkeypatch.setattr(bk, "INBOX_HUB", inbox)
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "config.json")
    # A2/P10: restore регистрирует в реестре — реестр в песочнице
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    # Б1: индексы add_config кладутся в дом configs/<cid> — корень тоже в
    # песочнице (иначе mkdir /data/configs на Linux-раннере — PermissionError)
    monkeypatch.setenv("COMPASS_CONFIGS_ROOT", str(tmp_path / "configs"))
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB", "COMPASS_ALIAS"):
        monkeypatch.delenv(var, raising=False)
    state._STATE = {}
    return backups, inbox


def _mini_index(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE methods (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT);
    CREATE TABLE calls (id INTEGER PRIMARY KEY);
    CREATE TABLE object_attributes (id INTEGER PRIMARY KEY);
    INSERT INTO methods VALUES (1, 'Ф1'), (2, 'Ф2');
    """)
    c.commit()
    c.close()


def test_create_backup_manifest_and_rotation(hubs, monkeypatch):
    backups, _ = hubs
    src = backups / "src.db"
    _mini_index(src)
    monkeypatch.setattr(bk, "BACKUP_KEEP", 2)
    reports = [bk.create_backup(None, "ut", src) for _ in range(4)]
    assert all(r.get("created") for r in reports)
    snaps = sorted((backups / "ut").iterdir())
    assert len(snaps) == 2  # keep=2
    manifest = json.loads((snaps[0] / "manifest.json").read_text("utf-8"))
    assert manifest["alias"] == "ut"
    assert manifest["index_counts"]["methods"] == 2
    # копия — рабочая база
    conn = sqlite3.connect(snaps[0] / "index.db")
    assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == 2
    conn.close()


def test_qdrant_absent_is_tolerated(hubs):
    """Qdrant не поднят/не настроен — слепок всё равно создаётся."""
    backups, _ = hubs
    src = backups / "src.db"
    _mini_index(src)
    report = bk.create_backup(None, "ut", src)
    assert report["created"] is True
    assert report["manifest"]["qdrant_snapshot"] is None


def test_inbox_scan_and_apply(hubs):
    """Принесённый слепок: найден, восстановлен, перенесён в архив.

    A2/P10: восстановление регистрирует конфигурацию в РЕЕСТРЕ (не в
    legacy state) и кладёт индекс на реестровый путь.
    """
    backups, inbox = hubs
    src = backups / "src.db"
    _mini_index(src)
    report = bk.create_backup(None, "kassa", src)
    snap = Path(report["dir"])
    # имитируем «принесли»: копия слепка в inbox
    brought = inbox / "UT_Kassa"
    brought.mkdir()
    for item in snap.iterdir():
        (brought / item.name).write_bytes(item.read_bytes())

    found = bk.scan_inbox()
    assert len(found) == 1 and found[0]["alias"] == "kassa"
    assert found[0]["has_index"] is True

    result = bk.apply_inbox_backup("UT_Kassa")
    assert result["restored"] == "kassa"
    assert not brought.exists()               # inbox пуст
    assert result["archived_to"].startswith(str(backups))
    # A2/P10: конфигурация в РЕЕСТРЕ, индекс на реестровом пути
    assert result["id"].startswith("Compass_kassa_")
    entry = reg.get_config(result["id"])
    assert entry["short_name"] == "kassa"
    assert Path(entry["index_db"]).exists()


def test_restore_rejects_alias_outside_short_name_rules(hubs):
    """Алиас манифеста вне правил short_name — честный отказ (A2/P10)."""
    import shutil
    backups, _ = hubs
    src = backups / "src.db"
    _mini_index(src)
    report = bk.create_backup(None, "ut", src)
    snap = Path(report["dir"])
    bad = backups / "бад"
    bad.mkdir()
    (bad / "manifest.json").write_text(
        json.dumps({"alias": "алиас-тест"}, ensure_ascii=False), "utf-8")
    shutil.copy2(snap / "index.db", bad / "index.db")
    result = bk.restore_backup(bad)
    assert "error" in result and "short_name" in result["error"]


def test_inbox_garbage_ignored(hubs):
    _, inbox = hubs
    (inbox / "мусор").mkdir()
    (inbox / "битый").mkdir()
    (inbox / "битый" / "manifest.json").write_text("{не json", "utf-8")
    assert bk.scan_inbox() == []


def test_restore_refuses_bad_and_reports(hubs):
    backups, _ = hubs
    empty = backups / "пусто"
    empty.mkdir()
    assert "error" in bk.restore_backup(empty)  # нет манифеста

    mf_only = backups / "толькоманифест"
    mf_only.mkdir()
    (mf_only / "manifest.json").write_text(
        json.dumps({"alias": "x"}), "utf-8")
    assert "index.db" in bk.restore_backup(mf_only)["error"]


def test_restore_rejects_foreign_config_id(hubs):
    """Аудит SAFE-4: слепок с чужим config_id поверх существующей
    конфигурации — отказ, файл индекса не заменён."""
    backups, _ = hubs
    result = reg.add_config("ut", "")
    assert "id" in result
    index_db = Path(result["entry"]["index_db"])
    index_db.parent.mkdir(parents=True, exist_ok=True)
    _mini_index(index_db)
    before = index_db.read_bytes()

    snap = backups / "ut" / "ut_fake"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text(json.dumps(
        {"alias": "ut", "config_id": "Compass_чужой_deadbeef"}),
        "utf-8")
    _mini_index(snap / "index.db")  # «подменная» база отличается содержимым
    conn = sqlite3.connect(snap / "index.db")
    conn.execute("INSERT INTO methods VALUES (99, 'ЗЛО')")
    conn.commit()
    conn.close()

    report = bk.restore_backup(snap)
    assert "error" in report and "другой конфигурации" in report["error"]
    assert index_db.read_bytes() == before  # индекс не тронут


def test_qdrant_snapshot_timeouts(hubs, monkeypatch):
    """F7/vlr: таймауты снапшота из env (create 600 / download 900 по
    умолчанию), скачивание записывает файл; сбой → None."""
    import urllib.error

    hub, _inbox = hubs
    captured = {}

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            # F7: copyfileobj читает до пустоты — отдаём payload ОДИН раз
            part = self._p
            self._p = b""
            return part

    def fake_urlopen(req, timeout=None):
        captured.setdefault("timeouts", []).append(timeout)
        # download в _qdrant_snapshot передаётся СТРОКОЙ-URL, create — Request
        is_download = isinstance(req, str) or req.method != "POST"
        if not is_download:
            return FakeResp(json.dumps({"result": {"name": "snap-1"}}).encode())
        return FakeResp(b"snapshot-bytes")

    monkeypatch.setattr(bk.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bk.config, "qdrant_url", "http://q:6333")
    target = hub / "snap.snapshot"
    name = bk._qdrant_snapshot("cfg_x_code", target)
    assert name == "snap-1"
    assert captured["timeouts"] == [600, 900]  # дефолты F7
    assert target.read_bytes() == b"snapshot-bytes"

    monkeypatch.setenv("QDRANT_SNAPSHOT_CREATE_TIMEOUT", "60")
    monkeypatch.setenv("QDRANT_SNAPSHOT_DOWNLOAD_TIMEOUT", "120")
    captured["timeouts"].clear()
    assert bk._qdrant_snapshot("cfg_x_code", target) == "snap-1"
    assert captured["timeouts"] == [60, 120]

    monkeypatch.setattr(bk.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            urllib.error.HTTPError(req.full_url, 500,
                                                   "boom", {}, None)))
    assert bk._qdrant_snapshot("cfg_x_code", target) is None


def test_restore_semantic_from_manifest(hubs, monkeypatch):
    """F7/vlr: restore_semantic — слепок ищется по dir_name, целевая
    коллекция — иммутабельное имя реестра (R2), multipart на
    /snapshots/upload; расхождение с манифестом → отказ; старый слепок
    (без qdrant_collection) → честная note."""
    hub, _inbox = hubs
    snap_dir = hub / "КА 2.5" / "ka_20261112T000000Z"
    snap_dir.mkdir(parents=True)
    (snap_dir / "qdrant.snapshot").write_bytes(b"payload")
    monkeypatch.setenv("QDRANT_SNAPSHOT_DOWNLOAD_TIMEOUT", "300")
    monkeypatch.setattr(bk.config, "qdrant_url", "http://q:6333")

    cid = reg.add_config("ka", "/dumps/ka",
                                 display_name="КА 2.5")["id"]
    reg.set_collection_name(cid, "cfg_КА_code")

    (snap_dir / "manifest.json").write_text(json.dumps({
        "alias": "КА 2.5", "qdrant_snapshot": "snap-1",
        "qdrant_collection": "cfg_КА_code"}), encoding="utf-8")

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b'{"result": true}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(bk.urllib.request, "urlopen", fake_urlopen)

    # расхождение манифест/реестр → отказ
    (snap_dir / "manifest.json").write_text(json.dumps({
        "alias": "КА 2.5", "qdrant_collection": "cfg_другая_code"}),
        encoding="utf-8")
    bad = bk.restore_semantic(cid, "ka_20261112T000000Z")
    assert "error" in bad and "cfg_другая_code" in bad["error"]
    assert "cfg_КА_code" in bad["error"]

    # совместимый манифест → upload на целевую коллекцию
    (snap_dir / "manifest.json").write_text(json.dumps({
        "alias": "КА 2.5", "qdrant_collection": "cfg_КА_code"}),
        encoding="utf-8")
    report = bk.restore_semantic(cid, "ka_20261112T000000Z")
    assert report["restored"] == "cfg_КА_code"
    assert "/collections/cfg_КА_code/snapshots/upload" in captured["url"]
    assert captured["timeout"] == 300
    assert b"payload" in captured["body"]
    assert b'name="snapshot"' in captured["body"]

    # старый слепок без qdrant_collection → note, целевая по реестру
    (snap_dir / "manifest.json").write_text(json.dumps({
        "alias": "КА 2.5", "qdrant_snapshot": "snap-1"}), encoding="utf-8")
    report = bk.restore_semantic(cid, "ka_20261112T000000Z")
    assert report["restored"] == "cfg_КА_code"
    assert "старый слепок" in report["note"]

    # слепок без снапшота и несуществующий
    empty = hub / "x" / "x_20261112T000000Z"
    empty.mkdir(parents=True)
    (empty / "manifest.json").write_text("{}", encoding="utf-8")
    assert "нет qdrant.snapshot" in bk.restore_semantic(
        cid, "x_20261112T000000Z")["error"]
    assert "не найден" in bk.restore_semantic(cid, "нет_такого")["error"]
