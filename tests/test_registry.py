# -*- coding: utf-8 -*-
"""Тесты реестра конфигураций (новый формат Compass_<short>_<uid>)
и переключения контекста."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as reg_mod  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Рабочая папка с реестром в tmp; env-пути убраны."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB"):
        monkeypatch.delenv(var, raising=False)
    ws._TEXT_CACHE.clear()
    return tmp_path


def _mini_index(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(f"""
    CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT, category TEXT,
        object_name TEXT, module_type TEXT, form_name TEXT, is_form INTEGER,
        mtime REAL, size INTEGER);
    CREATE TABLE methods (id INTEGER PRIMARY KEY, module_id INTEGER,
        name TEXT, type TEXT, is_export INTEGER, params TEXT,
        line INTEGER, end_line INTEGER, loc INTEGER);
    INSERT INTO modules VALUES (1, '{marker}.bsl', 'c', 'o', 'm', '', 0, 0, 0);
    INSERT INTO methods VALUES (1, 1, 'Ф_{marker}', 'Функция', 1, '()', 1, 2, 2);
    """)
    c.commit()
    c.close()


def test_add_generates_id_and_reads_info(workspace_dir):
    """add_config: ID Compass_<short>_<uid>, config_info из XML, дубликаты отклонены."""
    src = workspace_dir / "dumps" / "УТ"
    src.mkdir(parents=True)
    (src / "Configuration.xml").write_text(
        "<Metadata><Properties><Name>УправлениеТорговлей</Name>"
        "<Version>11.5.22.129</Version></Properties></Metadata>", encoding="utf-8")

    r = reg_mod.add_config("UT", str(src), display_name="УТ 11.5")
    cid = r["id"]
    assert cid.startswith("Compass_UT_")
    entry = reg_mod.get_config(cid)
    assert entry["short_name"] == "UT"
    assert entry["display_name"] == "УТ 11.5"
    assert entry["config_info"]["Name"] == "УправлениеТорговлей"
    # F9/06k: дефолт индекса — volume /data/index/<cid>
    assert Path(entry["index_db"]).parts[-2:] == (cid, "index.db")

    # дубликат short_name
    dup = reg_mod.add_config("UT", str(src), display_name="дубль")
    assert "error" in dup
    # невалидный short_name (кириллица/пробел)
    bad = reg_mod.add_config("У Т", str(src))
    assert "error" in bad


def test_config_use_switches_context(workspace_dir):
    """config_use привязывает конфигурацию к контексту."""
    reg_mod.add_config("UT", "/dumps/ut")
    reg_mod.add_config("KA", "/dumps/ka")

    from src.facade import registry_tools
    ok = registry_tools.config_use("Compass_KA_" + _uid_of("KA"))
    assert ok["active"] == "Compass_KA_" + _uid_of("KA")
    bad = registry_tools.config_use("ghost")
    assert "error" in bad and "не найдена" in bad["error"]


def _uid_of(short: str) -> str:
    from src.workspace import load_registry
    reg = load_registry()
    for cid in reg["configs"]:
        if cid.startswith(f"Compass_{short}_"):
            return cid.split("_")[-1]
    raise AssertionError(short)


def test_reader_follows_active_config(workspace_dir):
    """Читатель открывает индекс активной конфигурации (переключение)."""
    # уникальные short_name: в реальном реестре накапливаются дубликаты
    # от прошлых прогонов — ID берём из add_config, не поиском по имени
    import uuid as _uuid
    ut_short = "UT_" + _uuid.uuid4().hex[:6]
    ka_short = "KA_" + _uuid.uuid4().hex[:6]
    # создать файлы мини-индексов: без них reader() вернёт None
    _mini_index(workspace_dir / "ut.db", "UT")
    _mini_index(workspace_dir / "ka.db", "KA")
    ut_cid = reg_mod.add_config(ut_short, "/dumps/ut",
                                index_db=str(workspace_dir / "ut.db"))["id"]
    ka_cid = reg_mod.add_config(ka_short, "/dumps/ka",
                                index_db=str(workspace_dir / "ka.db"))["id"]

    from src.facade import registry_tools, tools
    registry_tools.config_use(ut_cid)
    rows_ut = tools.reader().methods_page(limit=5)
    registry_tools.config_use(ka_cid)
    rows_ka = tools.reader().methods_page(limit=5)
    assert rows_ut[0]["name"] == "Ф_UT"
    assert rows_ka[0]["name"] == "Ф_KA"
    registry_tools.reset_runtime_caches()


def _uid_short(workspace_dir: Path, short: str) -> str:
    from src.workspace import load_registry
    reg = load_registry()
    matches = [cid for cid in reg["configs"]
               if reg["configs"][cid]["short_name"] == short]
    if not matches:
        raise AssertionError(f"short_name '{short}' не найден")
    return matches[0]


def test_update_config_display_and_mode(workspace_dir):
    reg_mod.add_config("UT", "/dumps/ut")
    cid = _cid_of("UT")
    r = reg_mod.update_config(cid, display_name="УТ 11.5", mode="full")
    assert r["updated"] == cid
    entry = reg_mod.get_config(cid)
    assert entry["display_name"] == "УТ 11.5" and entry["mode"] == "full"


def _cid_of(short: str) -> str:
    from src.workspace import load_registry
    reg = load_registry()
    for cid, e in reg["configs"].items():
        if e["short_name"] == short:
            return cid
    raise AssertionError(short)


def test_set_vectors(workspace_dir):
    """R4/A4: set_vectors пишет points={"code": N}, модель, размерность,
    время обновления в entry["vectors"]."""
    reg_mod.add_config("UT", "/dumps/ut")
    cid = _cid_of("UT")
    reg_mod.set_vectors(cid, 591, "bge-m3", 1024)
    vectors = reg_mod.get_config(cid)["vectors"]
    assert vectors["points"] == {"code": 591}
    assert vectors["model"] == "bge-m3"
    assert vectors["dimension"] == 1024
    assert vectors["updated_at"]
    # повтор — перезаписывает
    reg_mod.set_vectors(cid, 600, "bge-m3", 1024)
    assert reg_mod.get_config(cid)["vectors"]["points"] == {"code": 600}


def test_migrate_default(workspace_dir, tmp_path, monkeypatch):
    """R12/A23: миграция маппит legacy-алиас active → созданный cid;
    default_config_id = cid, не алиас; мусор в active → None."""
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps({
        "configs": {"Atlas_UT_11_5": {"source_root": "/dumps/ut",
                                      "index_db": "/legacy/index/ut/index.db",
                                      "mode": "fast"},
                    "Atlas_KA_2_5": {"source_root": "/dumps/ka"}},
        "active": "Atlas_KA_2_5",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(reg_mod, "LEGACY_CONFIG_PATH", legacy)
    reg_mod.migrate_legacy_configs()

    reg = reg_mod.load_registry()
    assert len(reg["configs"]) == 2
    cid_ka = next(c for c, e in reg["configs"].items()
                  if e["display_name"] == "Atlas_KA_2_5 (мигрировано)")
    settings = ws.load_settings()
    assert settings["default_config_id"] == cid_ka
    assert reg.get("active_default") == cid_ka
    # legacy-файл переименован
    assert not legacy.exists() and Path(str(legacy) + ".migrated").exists()


def test_migrate_default_garbage_active(workspace_dir, tmp_path, monkeypatch):
    """active-алиас, которого нет в configs → default = None."""
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps({
        "configs": {"Atlas_UT_11_5": {"source_root": "/dumps/ut"}},
        "active": "НетТакого",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(reg_mod, "LEGACY_CONFIG_PATH", legacy)
    reg_mod.migrate_legacy_configs()
    assert ws.load_settings()["default_config_id"] is None
    assert reg_mod.load_registry().get("active_default") is None


def test_reset_keeps_indexing(workspace_dir):
    """R13/A24: сброс кэшей не трогает флаг indexing идущей векторизации
    (поток сам ставит False в finally); стек обнуляется."""
    from src.facade import registry_tools, semantic_tools
    monkey_state = {"collection": "c", "store": "s", "embeddings": "e",
                    "indexing": True, "last_stats": None}
    saved = semantic_tools._sem
    semantic_tools._sem = monkey_state
    try:
        registry_tools.reset_runtime_caches()
        assert monkey_state["indexing"] is True
        assert monkey_state["collection"] is None
        assert monkey_state["store"] is None
        assert monkey_state["embeddings"] is None
    finally:
        semantic_tools._sem = saved


def test_normalize_session_bindings(workspace_dir):
    """F1/zjp: миграция испорченных привязок — 'panel' и мусорные cid
    заменяются на валидный default; валидные записи и записи без
    config_id не трогаются."""
    from src import workspace as w
    cid = reg_mod.add_config("UT", "/dumps/ut")["id"]
    w.update_sessions(lambda d: d.setdefault("sessions", {}).update({
        "chat-ок": {"config_id": cid, "last_seen": "x"},   # валидная
        "chat-panel": {"config_id": "panel"},              # инверсия-мусор
        "chat-dead": {"config_id": "Compass_dead_deadbeef"},
        "chat-нет": {},                                    # без привязки
    }))
    settings = w.load_settings()
    settings["default_config_id"] = cid
    w.save_settings(settings)

    out = reg_mod.normalize_session_bindings()
    assert sorted(out["fixed"]) == ["chat-dead", "chat-panel"]
    assert out["default_used"] is True
    w.load_registry()  # обновить кэш реестра; читаем файл напрямую ниже:
    import json
    data = json.loads(w.sessions_path().read_text(encoding="utf-8"))
    sess = data["sessions"]
    assert sess["chat-ок"]["config_id"] == cid
    assert sess["chat-panel"]["config_id"] == cid
    assert sess["chat-dead"]["config_id"] == cid
    assert "config_id" not in sess["chat-нет"]


def test_normalize_no_valid_default_drops_field(workspace_dir):
    """default отсутствует/мусор → поле config_id удаляется (чат на дефолте)."""
    from src import workspace as w
    w.update_sessions(lambda d: d.setdefault("sessions", {}).update(
        {"chat-1": {"config_id": "panel"}}))
    settings = w.load_settings()
    settings["default_config_id"] = None
    w.save_settings(settings)
    out = reg_mod.normalize_session_bindings()
    assert out["default_used"] is False and out["fixed"] == ["chat-1"]
    import json
    data = json.loads(w.sessions_path().read_text(encoding="utf-8"))
    assert "config_id" not in data["sessions"]["chat-1"]


def test_build_paths_by_cid(workspace_dir, tmp_path, monkeypatch):
    """F2/pu9: пути сборки резолвятся из записи реестра по cid — первая
    сборка конфигурации без индекса не падает; env приоритетнее."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Configuration.xml").write_text("<x/>", encoding="utf-8")
    cid = reg_mod.add_config("UT", str(src),
                             index_db=str(tmp_path / "new" / "index.db")
                             )["id"]

    out = reg_mod.build_paths(cid)
    assert out["cid"] == cid
    assert out["source_root"] == src
    assert out["index_db"] == tmp_path / "new" / "index.db"  # не контекстный!
    assert out["display_name"] == "UT"

    # env COMPASS_* приоритетнее (dev-режим)
    monkeypatch.setenv("COMPASS_SOURCE_ROOT", str(src))
    monkeypatch.setenv("COMPASS_INDEX_DB", str(tmp_path / "dev.db"))
    out2 = reg_mod.build_paths(cid)
    assert out2["index_db"] == tmp_path / "dev.db"
    monkeypatch.delenv("COMPASS_SOURCE_ROOT")
    monkeypatch.delenv("COMPASS_INDEX_DB")

    # мусорный cid и не-выгрузка
    assert "error" in reg_mod.build_paths("Compass_xxx_00000000")
    bad = tmp_path / "bad"
    bad.mkdir()
    cid2 = reg_mod.add_config("KA", str(bad))["id"]
    assert "error" in reg_mod.build_paths(cid2)


def test_move_index_to_volume(workspace_dir, tmp_path, monkeypatch):
    """F9/06k + S1: перенос индекса в volume — файл переезжает, реестр
    правится; сборка/занятый путь/env — честные отказы. Дефолт новых
    конфигураций с S1 — дом configs/<cid>/index.db (arch-storage v0.2 §2);
    move_index_to_volume остаётся для legacy-индексов (их переносит в
    дома S4, до неё — volume-путь валиден)."""
    from src import state
    monkeypatch.setattr(reg_mod, "INDEX_ROOT", tmp_path / "data" / "index")

    # дефолт новых конфигураций — дом (S1), не /data/index
    cid = reg_mod.add_config("UT", "/dumps/ut")["id"]
    assert reg_mod.get_config(cid)["index_db"] == \
        str(reg_mod.config_index_db(cid))

    # перенос существующего (вне volume)
    old = tmp_path / "old" / "index.db"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"sqlite-bytes")
    reg_tmp = reg_mod.load_registry()
    reg_tmp["configs"][cid]["index_db"] = str(old)
    reg_mod.save_registry(reg_tmp)

    out = reg_mod.move_index_to_volume(cid)
    new_path = tmp_path / "data" / "index" / cid / "index.db"
    assert out["to"] == str(new_path)
    assert new_path.read_bytes() == b"sqlite-bytes"
    assert not old.exists()
    assert reg_mod.get_config(cid)["index_db"] == str(new_path)

    # повтор — уже в volume
    assert "уже в volume" in reg_mod.move_index_to_volume(cid)["info"]

    # занятый целевой путь
    cid2 = reg_mod.add_config("KA", "/dumps/ka")["id"]
    reg_tmp = reg_mod.load_registry()
    reg_tmp["configs"][cid2]["index_db"] = str(tmp_path / "other" / "index.db")
    reg_mod.save_registry(reg_tmp)
    Path(tmp_path / "other" / "index.db").parent.mkdir(parents=True)
    Path(tmp_path / "other" / "index.db").write_bytes(b"x")
    new2 = reg_mod.INDEX_ROOT / cid2 / "index.db"
    new2.parent.mkdir(parents=True)
    new2.write_bytes(b"y")
    assert "занят" in reg_mod.move_index_to_volume(cid2)["error"]

    # идёт сборка
    state.BUILD_STATE["building"] = True
    try:
        assert "идёт сборка" in reg_mod.move_index_to_volume(cid2)["error"]
    finally:
        state.BUILD_STATE["building"] = False

    # мусорный cid
    assert "не найдена" in reg_mod.move_index_to_volume(
        "Compass_xxx_00000000")["error"]
