# -*- coding: utf-8 -*-
"""compass-6z4: smoke-тесты scripts/reindex.py (моки subprocess/реестра).

Проверяются контрактные вещи: последовательность шагов конвейера reindex,
add → add_config + копирование выгрузки в дом, EXIT-парсер лога, отказ
--vectorize без подтверждения, битый EXIT → ошибка без пост-шагов.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reindex  # noqa: E402
from src import workspace as ws  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Герметичное окружение: системная зона, дома конфигураций, хаб дампов."""
    monkeypatch.setenv("COMPASS_SYSTEM_ROOT", str(tmp_path / "system"))
    monkeypatch.setenv("COMPASS_CONFIGS_ROOT", str(tmp_path / "configs"))
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(tmp_path / "Settings"))
    monkeypatch.setenv("DUMPS_HUB", str(tmp_path / "dumps"))
    (tmp_path / "Settings").mkdir()
    for var in ("COMPASS_SOURCE_ROOT", "COMPASS_INDEX_DB"):
        monkeypatch.delenv(var, raising=False)
    ws._TEXT_CACHE.clear()
    return tmp_path


def _patch_build_ok(monkeypatch, order, root):
    """Сборка идёт, воркер сразу пишет EXIT=0 в лог (микроподмена Popen).

    root — каталог песочницы (tmp_path фикстуры env): spawn_build делает
    mkdir родителя index_db, путь обязан быть записываемым (Б1)."""
    monkeypatch.setattr(reindex.registry_module, "build_paths",
                        lambda cid: {"cid": cid,
                                     "source_root": root / "src",
                                     "index_db": root / "index.db",
                                     "display_name": "X"})
    monkeypatch.setattr(reindex.registry_module, "ensure_ext_link",
                        lambda cid: (order.append("ext_link"),
                                     {"skipped": "стек пуст"})[1])

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
        order.append(("popen", tuple(cmd), start_new_session))
        stdout.write(b"[gyrfalcon] ok\nEXIT=0\n")
        stdout.flush()
        return object()

    monkeypatch.setattr(reindex.subprocess, "Popen", fake_popen)


def test_reindex_step_sequence(env, monkeypatch, capsys):
    """Конвейер reindex: пути → ext-симлинк → отвязанный воркер → тела →
    реестр → прогрев; итог JSON со статусом ok и путём лога."""
    order: list = []
    _patch_build_ok(monkeypatch, order, env)
    monkeypatch.setattr(reindex.bodies_module, "fill_bodies",
                        lambda db, src: (order.append("bodies"),
                                         {"status": "filled", "bodies": 3,
                                          "elapsed_s": 0.1})[1])
    monkeypatch.setattr(reindex, "_collect_counts",
                        lambda db: (order.append("counts"),
                                    {"methods": 2})[1])
    monkeypatch.setattr(reindex.registry_module, "set_indexed",
                        lambda cid, counts=None:
                        order.append(("indexed", cid, counts)))
    monkeypatch.setattr(reindex, "warm_qdrant",
                        lambda cid: (order.append("warm"),
                                     {"updated": True})[1])

    rc = reindex.main(["reindex", "Compass_X_0123abcd"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"

    kinds = [o if isinstance(o, str) else o[0] for o in order]
    assert kinds == ["ext_link", "popen", "bodies", "counts", "indexed",
                     "warm"]
    # воркер отвязанный (start_new_session) и оборачивается маркером EXIT
    popen_cmd, session = order[1][1], order[1][2]
    assert session is True
    assert popen_cmd[0] == "sh" and popen_cmd[1] == "-c"
    assert "gyrfalcon build" in popen_cmd[2] and "echo EXIT=$?" in popen_cmd[2]
    assert order[4] == ("indexed", "Compass_X_0123abcd", {"methods": 2})
    # лог существует и содержит маркер; путь отдаётся в отчёте
    log = Path(out["log_path"])
    assert log.exists() and "EXIT=0" in log.read_text(encoding="utf-8")
    assert log.parent == Path(env) / "system" / "logs"
    assert [s["step"] for s in out["steps"]] == [
        "paths", "build", "bodies", "indexed", "qdrant_warmup"]


def test_reindex_warmup_skipped_when_qdrant_stopped(env, monkeypatch, capsys):
    """--qdrant-stopped: прогрев не делается — честная пометка в шаге."""
    order: list = []
    _patch_build_ok(monkeypatch, order, env)
    monkeypatch.setattr(reindex.bodies_module, "fill_bodies",
                        lambda db, src: {"status": "filled"})
    monkeypatch.setattr(reindex, "_collect_counts", lambda db: None)
    monkeypatch.setattr(reindex.registry_module, "set_indexed",
                        lambda cid, counts=None: None)

    def no_warm(cid):  # прогрев при остановленном qdrant недопустим
        raise AssertionError("warm_qdrant вызван при --qdrant-stopped")

    monkeypatch.setattr(reindex, "warm_qdrant", no_warm)
    rc = reindex.main(["reindex", "Compass_X_0123abcd", "--qdrant-stopped"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    warm = out["steps"][-1]
    assert warm["step"] == "qdrant_warmup" and "skipped" in warm


def test_parse_exit_code():
    """EXIT-парсер: последний маркер; мусор/отсутствие — None."""
    assert reindex.parse_exit_code("[build] ...\nEXIT=0\n") == 0
    assert reindex.parse_exit_code("EXIT=1\nEXIT=0") == 0
    assert reindex.parse_exit_code("EXIT=1") == 1
    assert reindex.parse_exit_code("без маркера") is None
    assert reindex.parse_exit_code("EXIT=abc") is None  # битый — не маркер


def _patch_build_exit(monkeypatch, order, root, marker: bytes):
    """Воркер с заданным выводом (EXIT=1 — битый/неуспешный прогон);
    root — песочница (mkdir родителя index_db в spawn_build, Б1)."""
    monkeypatch.setattr(reindex.registry_module, "build_paths",
                        lambda cid: {"cid": cid,
                                     "source_root": root / "src",
                                     "index_db": root / "index.db",
                                     "display_name": "X"})
    monkeypatch.setattr(reindex.registry_module, "ensure_ext_link",
                        lambda cid: {"skipped": "стек пуст"})

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
        order.append("popen")
        stdout.write(marker)
        stdout.flush()
        return object()

    monkeypatch.setattr(reindex.subprocess, "Popen", fake_popen)


def test_nonzero_exit_stops_pipeline(env, monkeypatch, capsys):
    """EXIT=1: статус build_failed, пост-шаги (тела/реестр/прогрев) не идут."""
    order: list = []
    _patch_build_exit(monkeypatch, order, env, b"[gyrfalcon] panic\nEXIT=1\n")
    monkeypatch.setattr(reindex.bodies_module, "fill_bodies",
                        lambda db, src: order.append("bodies"))
    monkeypatch.setattr(reindex.registry_module, "set_indexed",
                        lambda cid, counts=None: order.append("indexed"))
    monkeypatch.setattr(reindex, "warm_qdrant",
                        lambda cid: order.append("warm"))

    rc = reindex.main(["reindex", "Compass_X_0123abcd"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "build_failed" and out["exit_code"] == 1
    assert "bodies" not in order and "indexed" not in order \
        and "warm" not in order


def test_worker_timeout_is_error(env, monkeypatch, capsys):
    """Маркера нет за таймаут: статус timeout, пост-шаги не идут; sleep не
    тормозит тест (таймаут 0.05 с, воркер остаётся жить — см. лог)."""
    order: list = []
    _patch_build_exit(monkeypatch, order, env, b"[gyrfalcon] building...\n")
    monkeypatch.setattr(reindex.time, "sleep", lambda s: None)
    monkeypatch.setattr(reindex, "BUILD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(reindex.registry_module, "set_indexed",
                        lambda cid, counts=None: order.append("indexed"))

    rc = reindex.main(["reindex", "Compass_X_0123abcd"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "timeout"
    assert "indexed" not in order


def _make_dump(env: Path) -> Path:
    dump = env / "dumps" / "KA25"
    (dump / "src").mkdir(parents=True)
    (dump / "Configuration.xml").write_text("<c/>", encoding="utf-8")
    (dump / "src" / "a.bsl").write_text("x", encoding="utf-8")
    return dump


def test_add_registers_and_copies_source(env, monkeypatch, capsys):
    """add: add_config с дом-схемой (source_root=None), копирование выгрузки
    в /data/configs/<cid>/source, bind_default, дальше конвейер."""
    _make_dump(env)
    calls: dict = {}

    def fake_add(short_name, source_root=None, display_name="", mode="fast",
                 index_db=None):
        calls["args"] = (short_name, source_root, display_name)
        return {"id": "Compass_KA25_ab12cd34", "entry": {}}

    monkeypatch.setattr(reindex.registry_module, "add_config", fake_add)
    monkeypatch.setattr(reindex.registry_module, "all_configs", lambda: [])
    monkeypatch.setattr(reindex, "bind_default",
                        lambda cid: calls.setdefault("bind", cid))

    def fake_pipeline(cid, *, qdrant_stopped=False, vectorize=False):
        calls["pipe"] = (cid, qdrant_stopped, vectorize)
        return {"status": "ok", "steps": []}

    monkeypatch.setattr(reindex, "run_pipeline", fake_pipeline)

    rc = reindex.main(["add", "KA25", "--from-dumps", "KA25"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # add_config: alias + дом-схема (source_root=None); display из XML пуст
    assert calls["args"] == ("KA25", None, "")
    assert calls["bind"] == "Compass_KA25_ab12cd34"
    assert calls["pipe"] == ("Compass_KA25_ab12cd34", False, False)
    # выгрузка реально скопирована в дом конфигурации
    copied = env / "configs" / "Compass_KA25_ab12cd34" / "source"
    assert (copied / "Configuration.xml").exists()
    assert (copied / "src" / "a.bsl").exists()
    assert out["added"] == "Compass_KA25_ab12cd34"
    assert out["alias"] == "KA25"
    assert out["status"] == "ok"


def test_add_rejects_bad_dump(env, monkeypatch, capsys):
    """Не выгрузка (нет Configuration.xml) — отказ до регистрации."""
    (env / "dumps" / "broken").mkdir(parents=True)
    registered = []
    monkeypatch.setattr(reindex.registry_module, "add_config",
                        lambda *a, **k: registered.append(a))
    rc = reindex.main(["add", "broken", "--from-dumps", "broken"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "Configuration.xml" in out["error"]
    assert not registered


def test_add_vectorize_requires_confirmation(env, monkeypatch, capsys):
    """--vectorize без --yes-vectorize — отказ ДО любых изменений."""
    _make_dump(env)
    touched: list = []
    monkeypatch.setattr(reindex.registry_module, "add_config",
                        lambda *a, **k: touched.append("add"))
    monkeypatch.setattr(reindex, "run_pipeline",
                        lambda *a, **k: touched.append("pipe"))

    rc = reindex.main(["add", "KA25", "--from-dumps", "KA25", "--vectorize"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused"
    assert "× 11" in out["warning"] or "11 токенов" in out["warning"]
    assert not touched  # ни реестр, ни конвейер не тронуты
    # выгрузка не копировалась
    assert not (env / "configs").exists()


def test_add_alias_duplicate_is_refused(env, monkeypatch, capsys):
    """Занятый alias (отказ add_config) — ошибка, конвейер не стартует."""
    _make_dump(env)
    monkeypatch.setattr(reindex.registry_module, "add_config",
                        lambda *a, **k: {"error": "short_name 'KA25' "
                                                  "уже используется"})
    monkeypatch.setattr(reindex.registry_module, "all_configs", lambda: [])
    piped: list = []
    monkeypatch.setattr(reindex, "run_pipeline",
                        lambda *a, **k: piped.append(a))

    rc = reindex.main(["add", "KA25", "--from-dumps", "KA25"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "уже используется" in out["error"]
    assert not piped


def test_stop_qdrant_flag_prints_hint(env, monkeypatch, capsys):
    """--stop-qdrant без подтверждения: подсказка и выход, сборка не идёт."""
    order: list = []
    _patch_build_ok(monkeypatch, order, env)
    rc = reindex.main(["reindex", "Compass_X_0123abcd", "--stop-qdrant"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "stop_qdrant_required"
    assert "docker stop bsl-compass-pro-qdrant" in out["hint"]
    assert "--qdrant-stopped" in out["hint"]
    assert not order  # конвейер не запускался


def test_spawn_vectorize_worker_detached(env, monkeypatch):
    """Воркер векторизации: отвязанный subprocess с этим же скриптом."""
    seen: dict = {}

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
        seen["cmd"] = tuple(cmd)
        seen["session"] = start_new_session
        return object()

    monkeypatch.setattr(reindex.subprocess, "Popen", fake_popen)
    log = env / "system" / "logs" / "x.log"
    log.parent.mkdir(parents=True)
    reindex.spawn_vectorize_worker("Compass_KA25_ab12cd34", log)
    assert seen["session"] is True
    assert seen["cmd"][0] == sys.executable
    assert seen["cmd"][2] == "_vectorize-worker"
    assert seen["cmd"][3] == "Compass_KA25_ab12cd34"


# ------------------------------------------- vectorize / ext-add (compass-8m1)

_CID = "Compass_KA25_ab12cd34"


def _patch_vec_registry(monkeypatch, entry):
    """Реестр для vectorize: запись конфигурации + имя коллекции."""
    monkeypatch.setattr(reindex.registry_module, "get_config",
                        lambda cid: entry)
    monkeypatch.setattr(reindex.registry_module, "collection_name",
                        lambda cid: f"cfg_{cid}_code")


def _patch_spawn_worker(monkeypatch, seen, marker: str | None = None):
    """Подменённый spawn_vectorize_worker: фиксирует вызов, опционально
    сразу пишет в лог финальный маркер воркера (межпроцессный финал)."""
    def fake_spawn(cid, log_path):
        seen["spawn"] = (cid, Path(log_path))
        if marker is not None:
            log_path.write_text(marker, encoding="utf-8")

    monkeypatch.setattr(reindex, "spawn_vectorize_worker", fake_spawn)


def test_vectorize_requires_confirmation(env, monkeypatch, capsys):
    """vectorize без --yes-vectorize — отказ ДО старта воркера."""
    _patch_vec_registry(monkeypatch, {"index_counts": {"methods": 100}})
    _patch_spawn_worker(monkeypatch, {})
    rc = reindex.main(["vectorize", _CID])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused"
    assert "100 × 11" in out["warning"]


def test_vectorize_unknown_cid(env, monkeypatch, capsys):
    """Конфигурации нет в реестре — честная ошибка, воркер не стартует."""
    spawned: list = []
    monkeypatch.setattr(reindex.registry_module, "get_config",
                        lambda cid: None)
    monkeypatch.setattr(reindex, "spawn_vectorize_worker",
                        lambda cid, log: spawned.append(cid))
    rc = reindex.main(["vectorize", _CID, "--yes-vectorize"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "не найдена" in out["error"]
    assert not spawned


def test_vectorize_starts_worker(env, monkeypatch, capsys):
    """Happy path: воркер запущен, отчёт started с оценкой токенов и
    логом под префиксом vectorize_."""
    seen: dict = {}
    _patch_vec_registry(monkeypatch, {"index_counts": {"methods": 100}})
    _patch_spawn_worker(monkeypatch, seen)
    rc = reindex.main(["vectorize", _CID, "--yes-vectorize"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "started"
    assert out["cid"] == _CID and out["methods"] == 100
    assert out["token_estimate"] == 100 * 11
    assert out["collection"] == f"cfg_{_CID}_code"
    assert seen["spawn"][0] == _CID
    assert Path(out["log_path"]).name.startswith(f"vectorize_{_CID}_")
    assert Path(out["log_path"]).parent == Path(env) / "system" / "logs"


def test_vectorize_wait_warm_done(env, monkeypatch, capsys):
    """--wait-warm: поллинг лога до финального маркера воркера; points —
    из реестра (его по завершении пишет сам воркер)."""
    seen: dict = {}
    entry = {"index_counts": {"methods": 7},
             "vectors": {"points": {"code": 42}, "model": "test-model"}}
    _patch_vec_registry(monkeypatch, entry)
    _patch_spawn_worker(monkeypatch, seen,
                        marker='{"status": "vectorize_done", "stats": {}}')
    monkeypatch.setattr(reindex.time, "sleep", lambda s: None)
    rc = reindex.main(["vectorize", _CID, "--yes-vectorize", "--wait-warm",
                       "5"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["vectorize"] == "done"
    assert out["points"] == 42 and out["model"] == "test-model"
    assert out["wait"]["result"] == "done"


def test_vectorize_wait_warm_failed(env, monkeypatch, capsys):
    """Воркер отчитался провалом — rc 1, ошибка отсылает к логу."""
    _patch_vec_registry(monkeypatch, {"index_counts": {"methods": 7}})
    _patch_spawn_worker(monkeypatch, {},
                        marker='{"status": "vectorize_failed"}')
    monkeypatch.setattr(reindex.time, "sleep", lambda s: None)
    rc = reindex.main(["vectorize", _CID, "--yes-vectorize", "--wait-warm",
                       "5"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "vectorize_failed"
    assert out["wait"]["result"] == "failed"


def test_vectorize_wait_warm_timeout(env, monkeypatch, capsys):
    """Маркера нет за потолок: честный timeout, воркер продолжает жить
    (status остаётся started, rc 0)."""
    _patch_vec_registry(monkeypatch, {"index_counts": {"methods": 7}})
    _patch_spawn_worker(monkeypatch, {})  # лог пуст — маркера не будет
    monkeypatch.setattr(reindex.time, "sleep", lambda s: None)
    rc = reindex.main(["vectorize", _CID, "--yes-vectorize", "--wait-warm",
                       "0.05"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "started"
    assert out["wait"]["result"] == "timeout"
    assert "воркер продолжает работать" in out["wait"]["note"]


def _patch_ext_add(monkeypatch, result, seen=None):
    def fake_ext_add(name, position=0, purpose="", status="stable",
                     source=""):
        if seen is not None:
            seen["args"] = (name, position, purpose, status, source)
        return result

    monkeypatch.setattr(reindex, "extension_add", fake_ext_add)


def test_ext_add_ok(env, monkeypatch, capsys):
    """happy path: параметры уходят в тур как есть, статус расширения
    (stable) переносится в ext_status, подсказка про reindex — в отчёте."""
    seen: dict = {}
    _patch_ext_add(monkeypatch, {"added": "Экз", "name": "Экз",
                                 "position": 1, "purpose": "Addition",
                                 "status": "stable"}, seen)
    monkeypatch.setattr(reindex, "active_config_id", lambda: _CID)
    rc = reindex.main(["ext-add", "Экз", "--position", "1",
                       "--purpose", "Дополнение"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert seen["args"] == ("Экз", 1, "Дополнение", "stable", "")
    assert out["status"] == "ok" and out["ext_status"] == "stable"
    assert out["cid"] == _CID
    assert f"reindex {_CID}" in out["next"]


def test_ext_add_hint_on_bad_position(env, monkeypatch, capsys):
    """position=0 — тур отвечает подсказкой директивы v0.4 (не ошибка)."""
    _patch_ext_add(monkeypatch, {"hint": "добавление расширения требует "
                                         "позиции и назначения",
                                 "name": "Экз"})
    rc = reindex.main(["ext-add", "Экз", "--position", "0",
                       "--purpose", "Дополнение"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "hint" and out["hint"]


def test_ext_add_error(env, monkeypatch, capsys):
    """Ошибка тура (нет активной конфигурации) — rc 2."""
    _patch_ext_add(monkeypatch, {"error": "активная конфигурация не выбрана "
                                          "— config_use"})
    rc = reindex.main(["ext-add", "Экз", "--position", "1",
                       "--purpose", "Дополнение"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert "config_use" in out["error"]


def test_ext_add_source_variants(env, monkeypatch, capsys):
    """--source: hub → "hub", путь → путь, флаг без значения → ""
    (исходники на месте — как у тура)."""
    seen: dict = {}
    _patch_ext_add(monkeypatch, {"added": "Экз", "name": "Экз",
                                 "position": 1, "purpose": "Addition",
                                 "status": "stable"}, seen)
    monkeypatch.setattr(reindex, "active_config_id", lambda: _CID)
    for argv, src in ((["--source", "hub"], "hub"),
                      (["--source", "/ext-dumps/Экз"], "/ext-dumps/Экз"),
                      (["--source"], "")):
        assert reindex.main(["ext-add", "Экз", "--position", "1",
                             "--purpose", "Дополнение", *argv]) == 0
        capsys.readouterr()
        assert seen["args"][4] == src
