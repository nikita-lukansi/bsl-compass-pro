# -*- coding: utf-8 -*-
"""Т3-Ф5/compass-941.4: инкрементальная пересборка (gyrfalcon update).

Герметично: мини-индекс + дерево выгрузки; gyrfalcon подменяется
FakePopen — проверяется команда, BUILD_STATE-механизм (watchdog fc9),
refusal-ветки и diff по modules (size/mtime — ddl.rs v0.1.20).
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as registry_mod  # noqa: E402
from src import state as state_mod  # noqa: E402
from src import workspace as ws  # noqa: E402
from src.facade import tools as facade_tools  # noqa: E402
from src.recon.gyr_reader import GyrReader  # noqa: E402
from tests.mini_index import make_mini_db, make_source_tree  # noqa: E402


class FakePopen:
    """Подменá gyrfalcon: запоминает команду, отдаёт заданный результат."""

    calls: list[list[str]] = []

    def __init__(self, cmd, stdout=None, stderr=None, text=None):
        FakePopen.calls.append(list(cmd))
        self.pid = 4242
        self.returncode = FakePopen.result[0]
        self._out = FakePopen.result[1]

    def communicate(self, timeout=None):
        return self._out, ""


FakePopen.result = (0, "Переиндексировано модулей 1 (удалено 0)")


@pytest.fixture
def incr_env(tmp_path, monkeypatch):
    """Мини-индекс как индекс конфигурации + дерево выгрузки в tmp.

    Строки слоёв расширений (id > 100) из мини-базы убираются: у них нет
    файлов в дереве выгрузки, дифф считал бы их удалёнными — сценарий
    файла — базовая конфигурация. build_paths требует Configuration.xml.
    """
    db = make_mini_db(tmp_path)
    root = make_source_tree(tmp_path)
    (root / "Configuration.xml").write_text(
        "<MetaDataObject><Configuration><Properties><Name>КА</Name>"
        "</Properties></Configuration></MetaDataObject>", encoding="utf-8")
    conn = sqlite3.connect(db)
    # сценарий файла — базовая конфигурация: слои расширений (id > 100) и
    # добавленные Волной-1 сиды цепочки (id 3) убираем — их файлов в
    # дереве выгрузки нет, дифф считал бы их удалёнными
    conn.execute("DELETE FROM methods_fts WHERE rowid > 2")
    conn.execute("DELETE FROM methods WHERE id > 2")
    conn.execute("DELETE FROM modules WHERE id > 2")
    conn.commit()
    conn.close()
    facade_tools.reset_reader()
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    cid = registry_mod.add_config("KA", str(root), index_db=str(db))["id"]
    settings = ws.load_settings()
    settings["default_config_id"] = cid
    ws.save_settings(settings)
    FakePopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    reset_calls = []
    monkeypatch.setattr(facade_tools, "reset_reader",
                        lambda: reset_calls.append(True))
    yield {"cid": cid, "db": db, "root": root, "resets": reset_calls}
    facade_tools.reset_reader()
    state_mod.build_finish(0, [])  # BUILD_STATE чист между тестами


def _sync_row_sizes(db: Path, root: Path, mtime: float | None = None):
    """Строки modules сада под реальные файлы (size, опц. mtime) —
    иначе дифф считает всё изменённым (в сидах size=0)."""
    conn = sqlite3.connect(db)
    for rel, in conn.execute("SELECT rel_path FROM modules "
                             "WHERE rel_path NOT LIKE '../%'").fetchall():
        p = root / rel
        if not p.is_file():
            continue
        st = p.stat()
        conn.execute("UPDATE modules SET size = ?, mtime = ? WHERE "
                     "rel_path = ?", (st.st_size,
                                      mtime if mtime is not None
                                      else st.st_mtime, rel))
    conn.commit()
    conn.close()


# ------------------------------------------------------------ changed_modules

def test_changed_modules_fresh_when_sizes_match(incr_env):
    db, root = incr_env["db"], incr_env["root"]
    _sync_row_sizes(db, root)
    r = GyrReader(db)
    diff = r.changed_modules(root)
    r.close()
    assert diff["changed"] == [] and diff["deleted"] == []
    assert diff["unchanged"] == diff["checked"] == 2  # базовые модули сидов


def test_changed_modules_detects_edit_and_delete(incr_env):
    db, root = incr_env["db"], incr_env["root"]
    _sync_row_sizes(db, root)
    target = root / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(target.read_text(encoding="utf-8-sig") + "\n// правка\n",
                      encoding="utf-8-sig")
    (root / "Documents/ДокЗаказ/Ext/ManagerModule.bsl").unlink()
    r = GyrReader(db)
    diff = r.changed_modules(root)
    r.close()
    assert diff["changed"] == ["CommonModules/ТестМодуль/Ext/Module.bsl"]
    assert diff["deleted"] == ["Documents/ДокЗаказ/Ext/ManagerModule.bsl"]


def test_changed_modules_new_files_invisible(incr_env):
    """Новый файл (не в modules) дифф не находит — честная граница метода."""
    db, root = incr_env["db"], incr_env["root"]
    _sync_row_sizes(db, root)
    new = root / "CommonModules/Новый/Ext/Module.bsl"
    new.parent.mkdir(parents=True)
    new.write_text("Функция А() Экспорт\nКонецФункции\n", encoding="utf-8-sig")
    r = GyrReader(db)
    diff = r.changed_modules(root)
    r.close()
    assert diff["changed"] == [] and diff["deleted"] == []


def test_changed_modules_null_signatures_counts_as_changed(incr_env):
    """mtime+size NULL (не бывает у живого строителя) — изменённый:
    нечем сверить, перестраховка дешевле расходящегося индекса."""
    db, root = incr_env["db"], incr_env["root"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE modules SET size = NULL, mtime = NULL "
                 "WHERE rel_path NOT LIKE '../%'")
    conn.commit()
    conn.close()
    r = GyrReader(db)
    diff = r.changed_modules(root)
    r.close()
    assert len(diff["changed"]) == 2


def test_index_meta_value_tolerates_missing_key(incr_env):
    r = GyrReader(incr_env["db"])
    # в мини-схеме index_meta пуста с compass-kep (состав колонок под
    # метатестом test_ddl_meta): ключа нет → None, исключения нет
    assert r.index_meta_value("source_path") is None
    r.close()


# ------------------------------------------------------- incremental_rebuild

def test_incremental_fresh_no_subprocess(incr_env):
    _sync_row_sizes(incr_env["db"], incr_env["root"])
    out = registry_mod.incremental_rebuild(incr_env["cid"])
    assert out["status"] == "fresh" and out["checked"] == 2
    assert FakePopen.calls == []          # gyrfalcon не звался
    assert incr_env["resets"] == []       # reader не сбрасывали
    assert state_mod.BUILD_STATE["building"] is False


def test_incremental_updated_build_state_and_reset(incr_env):
    db, root, cid = incr_env["db"], incr_env["root"], incr_env["cid"]
    _sync_row_sizes(db, root)
    target = root / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(target.read_text(encoding="utf-8-sig") + "\n// x\n",
                      encoding="utf-8-sig")
    out = registry_mod.incremental_rebuild(cid)
    assert out["status"] == "updated" and out["changed_total"] == 1
    assert out["changed_files"] == ["CommonModules/ТестМодуль/Ext/Module.bsl"]
    assert FakePopen.calls and FakePopen.calls[0][:4] == [
        "gyrfalcon", "update", "--db", str(db)]
    assert FakePopen.calls[0][-1].endswith("Module.bsl")
    # механизм сборки общий с полной (fc9): состояние закрыто, PID сброшен
    assert state_mod.BUILD_STATE["building"] is False
    assert state_mod.BUILD_STATE["pid"] is None
    assert state_mod.BUILD_STATE["exit_code"] == 0
    assert incr_env["resets"] == [True]   # reader-кэш сброшен после сборки
    entry = registry_mod.get_config(cid)
    assert entry["indexed_at"]            # отметка о свежей сборке
    assert "новые файлы" in out["note"]   # честная граница диффа


def test_incremental_passes_deleted_files_to_update(incr_env):
    """Удалённый файл передаётся в update — тот убирает его из индекса
    (incremental.rs: пропавший_файл_убирается_из_индекса)."""
    root = incr_env["root"]
    _sync_row_sizes(incr_env["db"], root)
    (root / "Documents/ДокЗаказ/Ext/ManagerModule.bsl").unlink()
    out = registry_mod.incremental_rebuild(incr_env["cid"])
    assert out["status"] == "updated" and out["deleted_total"] == 1
    assert out["deleted_files"] == ["Documents/ДокЗаказ/Ext/ManagerModule.bsl"]


def test_incremental_reports_gyrfalcon_error(incr_env):
    _sync_row_sizes(incr_env["db"], incr_env["root"])
    target = incr_env["root"] / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(target.read_text(encoding="utf-8-sig") + "\n// x\n",
                      encoding="utf-8-sig")
    FakePopen.result = (1, "инкремент принимает только .bsl ...")
    try:
        out = registry_mod.incremental_rebuild(incr_env["cid"])
    finally:
        FakePopen.result = (0, "")
    assert "error" in out and out["exit_code"] == 1
    assert "только .bsl" in "".join(out["tail"])
    assert state_mod.BUILD_STATE["building"] is False  # не зависла


def test_incremental_stale_edges_hint(incr_env):
    """Строка ВНИМАНИЕ в выводе update → честная подсказка про unknown-рёбра
    (цена инкремента — число в отчёте gyrfalcon, main.rs cmd_update)."""
    _sync_row_sizes(incr_env["db"], incr_env["root"])
    target = incr_env["root"] / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(target.read_text(encoding="utf-8-sig") + "\n// x\n",
                      encoding="utf-8-sig")
    FakePopen.result = (0, "Переиндексировано модулей 1 (удалено 0)\n"
                           "ВНИМАНИЕ: 3 вызовов ... остались неразрешёнными")
    try:
        out = registry_mod.incremental_rebuild(incr_env["cid"])
    finally:
        FakePopen.result = (0, "")
    assert "полной сборкой" in out["stale_edges_hint"]


def test_incremental_refuses_while_building(incr_env):
    gen = state_mod.build_begin()
    try:
        out = registry_mod.incremental_rebuild(incr_env["cid"])
        assert "error" in out and "идёт сборка" in out["error"]
        assert FakePopen.calls == []
    finally:
        state_mod.build_finish(0, [], gen)


def test_incremental_refuses_while_kb_sync(incr_env, monkeypatch):
    """Гейт kb-sync по публичному хуку state.DOCS_BUSY (§7 №5, compass-c6z):
    слой справки ставит флаг на время fetch — реестр отказывает, не читая
    приватное _docs слоя."""
    from src import state as state_mod
    state_mod.DOCS_BUSY.set()
    try:
        out = registry_mod.incremental_rebuild(incr_env["cid"])
        assert "error" in out and "документации" in out["error"]
    finally:
        state_mod.DOCS_BUSY.clear()


def test_incremental_refuses_with_full_build_queue(incr_env):
    """Очередь полных сборок не пуста → инкремент не пролезает вне очереди
    (её разгребает только _run_build — fc-механизм одной сборки)."""
    ws.save_build_queue(["Compass_other_11111111"])
    try:
        out = registry_mod.incremental_rebuild(incr_env["cid"])
        assert "error" in out and "очередь" in out["error"]
        assert FakePopen.calls == []
    finally:
        ws.save_build_queue([])


def test_incremental_refuses_missing_index(incr_env):
    cid = incr_env["cid"]
    reg = registry_mod.load_registry()
    reg["configs"][cid]["index_db"] = str(incr_env["db"].parent / "no.db")
    registry_mod.save_registry(reg)
    out = registry_mod.incremental_rebuild(cid)
    assert "error" in out and "полная сборка" in out["error"]


def test_incremental_refuses_alien_source(incr_env):
    """source_path индекса ≠ выгрузка реестра → отказ: дифф шёл бы не против
    того дерева, куда update запишет (main.rs: чужой корпус подсунуть нельзя)."""
    db, cid = incr_env["db"], incr_env["cid"]
    conn = sqlite3.connect(db)
    # index_meta есть в мини-схеме с compass-kep — только вписать чужой ключ
    conn.execute("INSERT INTO index_meta VALUES ('source_path', 'D:/чужой/дамп')")
    conn.commit()
    conn.close()
    out = registry_mod.incremental_rebuild(cid)
    assert "error" in out and "source_path" in out["error"]


def test_incremental_unknown_cid(incr_env):
    assert "error" in registry_mod.incremental_rebuild("Compass_x_00000000")


def test_incremental_truncates_long_lists(incr_env, monkeypatch):
    """Много изменённых → списки в ответе обрезаны с честным _truncated."""
    db, root = incr_env["db"], incr_env["root"]
    _sync_row_sizes(db, root)           # базовые строки — свежие
    conn = sqlite3.connect(db)
    for i in range(60):
        conn.execute(
            "INSERT INTO modules (id, rel_path, category, object_name,"
            " module_type, form_name, is_form, mtime, size)"
            f" VALUES ({1000 + i}, 'Extra/F{i}.bsl', NULL, NULL, NULL,"
            " NULL, 0, NULL, 1)")
    conn.commit()
    conn.close()
    for i in range(60):
        p = root / f"Extra/F{i}.bsl"
        p.parent.mkdir(exist_ok=True)
        p.write_text("xx", encoding="utf-8-sig")  # size 2 ≠ size 1 в строках
    monkeypatch.setattr(registry_mod, "INCREMENTAL_LIST_CAP", 10)
    out = registry_mod.incremental_rebuild(incr_env["cid"])
    assert out["changed_total"] == 60
    assert len(out["changed_files"]) == 10
    assert out["_truncated"] is True and "10" in out["hint"]


def test_rebuild_incremental_tool_resolves_active(incr_env):
    """MCP-тул: пустой cid → активная конфигурация контекста."""
    _sync_row_sizes(incr_env["db"], incr_env["root"])
    out = facade_tools.rebuild_incremental()
    assert out["status"] == "fresh"
    assert out["cid"] == incr_env["cid"]


def test_rebuild_incremental_tool_without_config(monkeypatch, tmp_path):
    """Конфигурация не выбрана → честная ошибка no_config (как у других тулов)."""
    from src import context
    monkeypatch.setattr(context, "active_config_id", lambda: None)
    out = facade_tools.rebuild_incremental()
    assert "error" in out


def test_watchdog_sees_incremental_proc(incr_env, monkeypatch):
    """set_build_proc: PID gyrfalcon update попадает в BUILD_STATE на время
    сборки — watchdog (fc9) видит процесс, как у полной сборки."""
    seen: list = []
    real = state_mod.set_build_proc

    def spy(proc):
        real(proc)
        seen.append(state_mod.BUILD_STATE["pid"])

    monkeypatch.setattr(state_mod, "set_build_proc", spy)
    _sync_row_sizes(incr_env["db"], incr_env["root"])
    target = incr_env["root"] / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(target.read_text(encoding="utf-8-sig") + "\n// x\n",
                      encoding="utf-8-sig")
    registry_mod.incremental_rebuild(incr_env["cid"])
    assert seen == [4242]                 # PID FakePopen во время сборки
