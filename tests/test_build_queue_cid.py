# -*- coding: utf-8 -*-
"""R3/A3+A20: гейт 409 в api_config_use + очередь сборки на cid.

Механика сборки (drain/start/очередь) — AST-разбором main.py по образцу
test_main_smoke_names. Гейт api_config_use — поведенчески через
TestClient(mcp.http_app()) (fastmcp в requirements-dev, см.
test_http_smoke): 409 «идёт сборка индекса» при BUILD_STATE.building.
Панельные bind-точки — в файле панельных роутов (локатор
tests/panel_source.py: panel_api.py после выноса compass-2ao, до — main.py).
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_SRC = Path(__file__).parent.parent / "src" / "main.py"


def _tree():
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def _func(tree, name):
    return next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _calls(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                out.append(f.attr)
    return out


def test_drain_binds_registry_not_state():
    """Drain: bind_default/bind_context по cid + reset_runtime_caches;
    state.use_config отсутствует (переключение реестровое, R3/A20)."""
    drain = _func(_tree(), "_drain_build_queue")
    calls = _calls(drain)
    assert "bind_default" in calls
    assert "bind_context" in calls
    assert "reset_runtime_caches" in calls
    assert "_start_build_locked" in calls
    assert "use_config" not in calls
    # cid достаётся из очереди и проверяется по реестру
    src = ast.unparse(drain)
    assert "_build_queue.pop(0)" in src
    assert "get_config(cid)" in src


def test_start_build_enqueues_cid():
    """_start_build_locked: в очередь кладётся config.active_config_id."""
    start = _func(_tree(), "_start_build_locked")
    src = ast.unparse(start)
    assert "config.active_config_id" in src
    assert "_build_queue.append(cid)" in src
    assert "config.alias" not in src


def test_config_use_gate_409(monkeypatch):
    """api_config_use: 409 «идёт сборка индекса» при активной сборке
    (поведенчески: BUILD_STATE.building → POST /api/config/use)."""
    from starlette.testclient import TestClient

    from src import state
    from src.main import mcp
    monkeypatch.setattr(state, "BUILD_STATE", {"building": True})
    # base_url localhost — POST за Host-гейтом P1-1 (аудит 4l5)
    with TestClient(mcp.http_app(), base_url="http://localhost:8123") as client:
        r = client.post("/api/config/use", params={"id": "Compass_x_00"})
    assert r.status_code == 409
    assert "идёт сборка индекса; переключение заблокировано" in r.json()["error"]


def test_build_queue_is_cid_list():
    """Очередь объявлена как список cid (R3), не алиасов."""
    src = _SRC.read_text(encoding="utf-8")
    line = next(row for row in src.splitlines()
                if row.lstrip().startswith("_build_queue"))
    assert "list[str]" in line
    assert "# cid" in line


def test_build_queue_persist(tmp_path, monkeypatch):
    """R15/A16: очередь сохраняется в settings.json (merge) и читается
    обратно; восстановление в main() фильтрует cid, удалённые из реестра."""
    from src import registry as reg
    from src import workspace as ws

    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()

    cid = reg.add_config("ut", "/dumps/ut")["id"]
    ws.save_build_queue([cid, "Compass_мусор_00000000"])
    assert ws.load_build_queue() == [cid, "Compass_мусор_00000000"]

    # merge: существующие ключи settings не теряются
    ws.save_settings({"default_config_id": cid})
    ws.save_build_queue([cid])
    assert ws.load_settings()["default_config_id"] == cid

    # восстановление с валидацией — как в main()
    restored = [c for c in ws.load_build_queue() if reg.get_config(c)]
    assert restored == [cid]


def test_main_persists_and_restores_queue():
    """main.py: очередь сохраняется при push/drain и восстанавливается
    на старте с валидацией по реестру (AST-верификация, main не импортируется)."""
    src = _SRC.read_text(encoding="utf-8")
    assert "save_build_queue(_build_queue)" in src
    tree = ast.parse(src)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    src_main = ast.unparse(main_fn)
    assert "load_build_queue" in src_main and "get_config(cid)" in src_main


def test_bind_inversion_fixed():
    """F1/76h: инверсии bind_context устранены — панель привязывается к
    конфигурации (cid, PANEL_CONTEXT), не наоборот; bind_default шлёт
    config_id первым аргументом (AST-верификация context.py; панельные
    bind-роуты — в файле панельных роутов, см. tests/panel_source.py;
    корректность bind в _drain_build_queue — отдельным тестом выше)."""
    from tests.panel_source import panel_routes_source
    src = panel_routes_source().read_text(encoding="utf-8")
    assert "bind_context(PANEL_CONTEXT, cid)" not in src
    # A2/P10: реестровые bind в api_bootstrap/api_config_add/
    # api_data_register — инвариант гвара в порядке аргументов и минимуме
    # точек привязки, не в счёте
    assert src.count("bind_context(cid, PANEL_CONTEXT)") >= 2
    ctx_src = (_SRC.parent / "context.py").read_text(encoding="utf-8")
    tree = ast.parse(ctx_src)
    bind_default = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "bind_default")
    calls = [ast.unparse(c) for c in ast.walk(bind_default)
             if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name) and c.func.id == "bind_context"]
    assert calls == ["bind_context(config_id, PANEL_CONTEXT)"]
