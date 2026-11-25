# -*- coding: utf-8 -*-
"""Смок src/main.py без импорта (fastmcp может отсутствовать):
1) в каждой top-level функции все используемые имена определены
   (импорт/локаль/модуль/builtin) — ловит NameError-класс регрессий (P23);
2) в main.py нет __globals__-хаков (P24)."""
import ast
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_SRC = Path(__file__).parent.parent / "src" / "main.py"
_BUILTINS = set(dir(builtins))
_DUNDER = {"__file__", "__name__", "__doc__", "__annotations__",
           "__builtins__", "__package__", "__loader__", "__spec__",
           "__path__", "__dict__", "__qualname__"}


def _undefined_names(tree):
    module_names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)

    problems = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        locals_, loads = set(), set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                (locals_ if isinstance(sub.ctx, ast.Store) else loads).add(sub.id)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    locals_.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, ast.ClassDef):
                locals_.add(sub.name)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if hasattr(sub, "name"):
                    locals_.add(sub.name)
                args = sub.args
                for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                    locals_.add(a.arg)
                if args.vararg:
                    locals_.add(args.vararg.arg)
                if args.kwarg:
                    locals_.add(args.kwarg.arg)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                locals_.add(sub.name)
            elif isinstance(sub, ast.comprehension):
                for t in ast.walk(sub.target):
                    if isinstance(t, ast.Name):
                        locals_.add(t.id)
        args = node.args
        for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
            locals_.add(a.arg)
        if args.vararg:
            locals_.add(args.vararg.arg)
        if args.kwarg:
            locals_.add(args.kwarg.arg)
        undefined = loads - locals_ - module_names - _BUILTINS - _DUNDER
        if undefined:
            problems.append((node.name, sorted(undefined)))
    return problems


def test_no_undefined_names_in_main():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    problems = _undefined_names(tree)
    assert problems == [], f"неопределённые имена в main.py: {problems}"


def test_no_globals_hack_in_main():
    """Обращение к __globals__ чужого модуля — источник P24 (KeyError);
    после A18 его в main.py быть не должно."""
    assert "__globals__" not in _SRC.read_text(encoding="utf-8")


def test_status_uses_brief_runtime_not_ping():
    """P43: /api/status не должен звать runtime_status() — синхронный ping
    с таймаутом 120 с в async-роуте блокирует event loop на каждом опросе
    панели. Лёгкий _runtime_brief (mode/configured/hint) — без сети.
    (Роут — в файле панельных роутов, см. tests/panel_source.py.)"""
    from tests.panel_source import panel_routes_source
    src = panel_routes_source().read_text(encoding="utf-8")
    assert '"runtime": runtime_tools._runtime_brief()' in src


def test_main_starts_offline_registry_and_status_summary():
    """Т2-G: реестр офлайн-задач стартует в main() (watcher + восстановление
    из файлов при старте ядра), а /api/status читает готовую сводку
    offline_summary — не дёргает файловый скан из горячих статусов."""
    from tests.panel_source import panel_routes_source
    src = panel_routes_source().read_text(encoding="utf-8")
    assert "offline_summary" in src
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert any(isinstance(sub, ast.Call)
               and isinstance(sub.func, ast.Name)
               and sub.func.id == "get_registry"
               for sub in ast.walk(main_fn)), \
        "в main() нет вызова get_registry() (старт реестра в offline)"


def test_api_links_uses_to_thread():
    """R6/A7: api_links зовёт github_info в потоке (asyncio.to_thread) —
    сетевой вызов не блокирует event loop; в main() есть прогрев-поток.
    (api_links — в файле панельных роутов, см. tests/panel_source.py;
    прогрев-поток — в main.py.)"""
    from tests.panel_source import panel_routes_source
    tree = ast.parse(panel_routes_source().read_text(encoding="utf-8"))
    api_links = next(n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == "api_links")
    assert "to_thread" in ast.unparse(api_links)
    main_tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    main_fn = next(n for n in main_tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    src_main = ast.unparse(main_fn)
    assert "github_info" in src_main and "Thread" in src_main


def test_tools_no_sys_path_hack():
    """R14/A15: в tools.py нет sys.path.insert("/app")-хака."""
    tools_src = (Path(__file__).parent.parent / "src" / "facade"
                 / "tools.py").read_text(encoding="utf-8")
    assert "sys.path.insert" not in tools_src


def test_status_poll_does_not_touch_session_activity():
    """R14/A15: middleware не тикает session_activity на /api/status
    (poll панели); mcp и прочие api тикают."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    mw = next(n for n in ast.walk(tree)
              if isinstance(n, ast.ClassDef) and n.name == "ChatNameMiddleware")
    src = ast.unparse(mw)
    # ast.unparse нормализует кавычки — ищем без зависимости от них
    assert "/api/status" in src
    assert "touch" in src and "startswith" in src
