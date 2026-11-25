# -*- coding: utf-8 -*-
"""CLI-оркестратор компаса (S5, arch-storage v0.2): для агентов и владельца.

Запуск: python -m src.manage <команда> … — работает в контейнере и на
хосте (env-override путей как у ядра). Все команды печатают JSON
(ensure_ascii=False) — читается и человеком, и агентом.

Команды:
  add         конфигурация из хаба дампов: копия → configs/<cid>/source/ →
              индексация; опционально корпус ИТС (--docs) и расширения
              (--extension Имя:Тип:Позиция, многократно)
  reindex     пересборка индекса [CID | --config CID] (--incremental —
              точечная gyrfalcon update); полный прогон пишет метрики
              реестра (indexed_at + кэш счётчиков) как панельный /reindex
  extension   add / sync — расширения конфигурации
  help        sync SOURCE_ID — выгрузка корпуса ИТС в docs/<cid>/<source_id>
  status      сводка по домам и реестру
  migrate     migrate-to-homes (S4) — перенос legacy-конфигураций в дома

Код не импортирует src.main (fastmcp не нужен в CLI-окружении); сборка —
свой синхронный запуск gyrfalcon с тем же протоколом BUILD_STATE
(build_begin/finish + PID для watchdog'а).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _out(result: dict) -> dict:
    """Унифицированный вывод: JSON в stdout; exit-код по наличию error."""
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def _cid_or_active(args_cid: str | None) -> str | None:
    """cid из --config или активный контекст (панель-дефолт)."""
    if args_cid:
        return args_cid
    from .context import active_config_id
    return active_config_id()


def _resolve_dump(source: str) -> Path | None:
    """Путь выгрузки: имя папки хаба дампов или путь как есть.

    Точка входа — DUMPS_HUB (read-only скан; arch-storage §2).
    """
    from .state import DUMPS_HUB
    candidate = Path(source)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    in_hub = DUMPS_HUB / source
    if in_hub.is_dir():
        return in_hub
    if candidate.exists():
        return candidate
    return None


# ----------------------------------------------------------- сборка индекса

def build_index_now(cid: str, paths: dict) -> dict:
    """Синхронная сборка индекса gyrfalcon (CLI-версия _run_build из main).

    Тот же протокол BUILD_STATE (build_begin/finish, PID — watchdog fc9),
    ensure_ext_link (S1), кэш счётчиков и досье — как у полной сборки.
    Без notify/авто-бэкапа: владелец видит вывод CLI сам. Точка
    инъекции для тестов (monkeypatch на manage.build_index_now).
    """
    from . import registry as registry_module
    from . import state

    gen = state.build_begin()
    registry_module.ensure_ext_link(cid)
    tail: list[str] = []
    try:
        paths["index_db"].parent.mkdir(parents=True, exist_ok=True)
        cmd = ["gyrfalcon", "build", str(paths["source_root"]), "--out",
               str(paths["index_db"])]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        state.set_build_proc(proc)
        try:
            out, _ = proc.communicate(timeout=3600)
            tail = (out or "").strip().splitlines()
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            tail = ((out or "").strip().splitlines()
                    + ["timeout: gyrfalcon убит по таймауту 3600 c"])
            exit_code = proc.returncode if proc.returncode is not None else 1
    except Exception as exc:
        exit_code = 1
        tail = [f"{type(exc).__name__}: {exc}"]
    finally:
        state.build_finish(exit_code, tail, gen)

    if exit_code != 0:
        return {"error": "gyrfalcon build завершился с ошибкой",
                "exit_code": exit_code, "tail": tail[-8:]}

    registry_module.set_indexed(cid)
    try:
        from .facade.tools import reader, reset_reader
        reset_reader()
        registry_module.set_indexed(cid, reader().stats())
    except Exception as exc:
        tail.append(f"кэш счётчиков не записан: {type(exc).__name__}: {exc}")
    try:
        from .facade.dossier import build_docs as _build_docs
        _build_docs(cid)
    except Exception:
        pass  # досье не роняет сборку (как в main._run_build)
    return {"built": str(paths["index_db"]), "tail": tail[-8:]}


# ------------------------------------------------------------------- add

def _copy_extension(cid: str, spec: str) -> dict:
    """--extension Имя:Тип:Позиция → копия из хаба + запись стека.

    Имя — папка выгрузки расширения в DUMPS_HUB (и имя в стеке);
    Тип — Исправление/Адаптация/Дополнение (или Patch/Customization/
    Addition); Позиция — 1 = верх списка конфигуратора (директива v0.4).
    """
    from . import registry as registry_module

    parts = [p.strip() for p in spec.split(":")]
    if len(parts) != 3:
        return {"error": f"--extension '{spec}': формат Имя:Тип:Позиция"}
    name, purpose, position = parts
    src = _resolve_dump(name)
    if src is None:
        return {"error": f"--extension '{name}': папки нет в хабе дампов "
                         "(DUMPS_HUB) и по пути — проверьте имя"}
    dest = registry_module.config_extension_root(cid, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return {"error": f"--extension '{name}': "
                         f"{dest} уже существует"}
    shutil.copytree(src, dest, symlinks=True)
    added = registry_module.stack_add(cid, name, position, purpose)
    if "error" in added:
        shutil.rmtree(dest, ignore_errors=True)  # откат копии
        return added
    return {"extension": name, "copied": str(dest), **added}


def cmd_add(args) -> dict:
    """add: скан хаба → копия → реестр → (+расширения) → индексация."""
    from . import registry as registry_module

    src = _resolve_dump(args.source) if args.source else None
    if src is None and not args.source:
        from .state import scan_dumps
        return _out({"error": "укажите --source ПАПКА (имя в хабе дампов "
                              "или путь); доступно в хабе:",
                     "available": [d["alias"] for d in scan_dumps()]})
    if src is None:
        return _out({"error": f"выгрузка не найдена: {args.source}"})
    if not (src / "Configuration.xml").exists():
        return _out({"error": f"в {src} нет Configuration.xml — "
                              "это не выгрузка конфигурации"})

    result = registry_module.add_config(args.name, display_name=args.display)
    if "error" in result:
        return _out(result)
    cid = result["id"]

    # рабочая копия выгрузки в дом (спека §1: хост-дамп после этого можно
    # удалить); symlinks=True — не разыменовывать чужие симлинки
    dest = registry_module.config_source_root(cid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=True)
    # путь в реестре — дом (add_config с source_root=None дал бы тот же);
    # RMW под файл-локом (аудит PERF-6: гонка с ядром)
    try:
        with registry_module.registry_lock():
            reg = registry_module.load_registry()
            reg["configs"][cid]["source_root"] = str(dest)
            reg["configs"][cid]["source_version"] = (
                registry_module.source_version(dest))
            registry_module.save_registry(reg)
    except registry_module.RegistryBusy:
        return _out({"error": "реестр занят другим процессом"})

    out: dict = {"added": cid, "source_copy": str(dest),
                 "display_name": result["entry"]["display_name"]}
    if args.extension:
        ext_results = [_copy_extension(cid, spec) for spec in args.extension]
        errors = [e for e in ext_results if "error" in e]
        if errors:
            return _out({**out, "error": "расширения не добавлены",
                         "details": errors})
        out["extensions"] = ext_results

    if not args.no_build:
        paths = registry_module.build_paths(cid)
        if "error" in paths:
            return _out({**out, "error": paths["error"]})
        out["build"] = build_index_now(cid, paths)

    if args.docs:
        out["docs"] = cmd_help_sync(
            argparse.Namespace(command="help", subcommand="sync",
                               source_id=args.docs, config=cid,
                               corpus_only=False))
    return _out(out)


# ---------------------------------------------------------------- reindex

def cmd_reindex(args) -> dict:
    """reindex: полная или точечная (--incremental) пересборка."""
    from . import registry as registry_module

    cid = args.cid or _cid_or_active(args.config)
    if not cid:
        return _out({"error": "конфигурация не указана (позиционный CID или "
                              "--config) и активной нет"})
    if args.incremental:
        return _out(registry_module.incremental_rebuild(cid))
    paths = registry_module.build_paths(cid)
    if "error" in paths:
        return _out(paths)
    return _out({"cid": cid, **build_index_now(cid, paths)})


# -------------------------------------------------------------- extension

def cmd_extension(args) -> dict:
    """extension add|sync: подключение выгрузок расширений к конфигурации."""
    from . import registry as registry_module

    cid = _cid_or_active(args.config)
    if not cid:
        return _out({"error": "конфигурация не указана (--config) и активной "
                              "нет"})
    if args.subcommand == "add":
        return _out(_copy_extension(cid, args.spec))
    # sync: каталоги extensions/<cid> против стека реестра + симлинк
    ext_root = registry_module.config_extension_root(cid)
    on_disk = (sorted(p.name for p in ext_root.iterdir())
               if ext_root.is_dir() else [])
    stack = [e.get("name") for e in registry_module.stack_of(cid)]
    missing_in_stack = [n for n in on_disk if n not in stack]
    missing_on_disk = [n for n in stack if n not in on_disk]
    link = registry_module.ensure_ext_link(cid)
    return _out({"cid": cid, "on_disk": on_disk, "stack": stack,
                 "missing_in_stack": missing_in_stack,
                 "missing_on_disk": missing_on_disk, "ext_link": link})


# ------------------------------------------------------------------- help

def cmd_help_sync(args) -> dict:
    """help sync SOURCE_ID: корпус ИТС → docs/<cid>/<source_id> + kb sync."""
    from .facade.help_tools import sync_corpus
    from . import registry as registry_module

    cid = _cid_or_active(args.config)
    if not cid:
        return _out({"error": "конфигурация не указана (--config) и активной "
                              "нет"})
    entry = registry_module.get_config(cid)
    if entry is None:
        return _out({"error": f"конфигурация '{cid}' не найдена"})
    db = registry_module.config_index_db(cid)
    if not db.exists():
        return _out({"error": f"индекс не собран: {db} — сначала "
                              "python -m src.manage add/reindex"})
    try:
        stats = sync_corpus(args.source_id, cid, db=db)
    except Exception as exc:
        return _out({"error": f"{type(exc).__name__}: {exc}",
                     "hint": "креды ИТС — ITS_LOGIN/ITS_PASSWORD или "
                             "ITS_COOKIE в .env"})
    return _out({"cid": cid, "corpus": str(
        registry_module.config_docs_root(cid, args.source_id)), **stats})


# ----------------------------------------------------------------- status

def cmd_status(args) -> dict:
    """status: сводка по домам configs/docs/extensions и системной зоне."""
    from . import registry as registry_module
    from . import workspace

    homes = []
    for c in registry_module.all_configs():
        cid = c["id"]
        source = Path(c.get("source_root") or "")
        index_db = Path(c.get("index_db") or "")
        home = registry_module.config_home(cid)
        current_version = registry_module.source_version(source)
        saved_version = c.get("source_version")
        homes.append({
            "cid": cid,
            "display_name": c.get("display_name", ""),
            "home": str(home),
            "home_scheme": source.resolve() == registry_module \
                .config_source_root(cid).resolve(),
            "index_db": str(index_db),
            "index_exists": index_db.exists(),
            "index_bytes": index_db.stat().st_size if index_db.exists() else 0,
            "source_exists": source.is_dir(),
            "source_fresh": (saved_version == current_version
                             if saved_version is not None
                             and current_version is not None else None),
            "stack": [e.get("name") for e in c.get("stack") or []],
            "docs": sorted(p.name for p in
                           registry_module.docs_root().joinpath(cid).iterdir())
            if registry_module.docs_root().joinpath(cid).is_dir() else [],
        })
    return _out({
        "system": {
            "root": str(workspace.system_root()),
            "settings": str(workspace.settings_dir()),
            "registry_exists": workspace.registry_path().exists(),
        },
        "dumps_hub_entries": [d["alias"] for d in _scan()],
        "configs": homes,
        "active_hint": "python -m src.manage reindex --config CID",
    })


def _scan() -> list[dict]:
    from .state import scan_dumps
    return scan_dumps()


# ---------------------------------------------------------------- migrate

def cmd_migrate(args) -> dict:
    """migrate: перенос legacy-конфигураций в дома (реализация — S4)."""
    from . import registry as registry_module

    fn = getattr(registry_module, "migrate_to_homes", None)
    if fn is None:
        return _out({"error": "migrate-to-homes ещё не реализована (S4, "
                              "compass-0od.12) — команда заработает после "
                              "её коммита"})
    return _out(fn(args.config))


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    """Парсер CLI; русские --help (оркестратор для агентов и владельца)."""
    parser = argparse.ArgumentParser(
        prog="python -m src.manage",
        description="CLI-оркестратор BSL Compass Pro: дома configs/docs/"
                    "extensions, сборка индекса, документация ИТС. Вывод — "
                    "JSON (читается человеком и агентом).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser(
        "add", help="добавить конфигурацию из хаба дампов (копия в дом "
        "configs/<cid>/source + индексация)")
    p_add.add_argument("--name", required=True,
                       help="короткое имя (латиница/цифры/дефис, 1–16; "
                       "станет частью Compass_<name>_<uid>)")
    p_add.add_argument("--source", help="папка выгрузки: имя в хабе дампов "
                       "(DUMPS_HUB) или путь; без него — список доступных")
    p_add.add_argument("--display", default="",
                       help="отображаемое имя (дефолт — Name из "
                       "Configuration.xml)")
    p_add.add_argument("--extension", action="append", metavar="Имя:Тип:Позиция",
                       help="расширение из хаба дампов, тип — "
                       "Исправление|Адаптация|Дополнение (или "
                       "Patch|Customization|Addition), позиция — 1 = верх "
                       "списка конфигуратора; флаг многократный")
    p_add.add_argument("--docs", metavar="SOURCE_ID",
                       help="после сборки выгрузить корпус ИТС "
                       "(например ut11doc) в docs/<cid>/<source_id>")
    p_add.add_argument("--no-build", action="store_true",
                       help="не запускать индексацию (только копия+реестр)")

    p_re = sub.add_parser(
        "reindex", help="пересобрать индекс активной/указанной конфигурации")
    p_re.add_argument("cid", nargs="?", metavar="CID",
                      help="cid конфигурации (то же, что --config; "
                      "по умолчанию — активная конфигурация панели)")
    p_re.add_argument("--config", help="cid (альтернативный синтаксис; "
                      "дефолт — активная конфигурация панели)")
    p_re.add_argument("--incremental", action="store_true",
                      help="точечная пересборка (gyrfalcon update; полная "
                      "нужна для новых файлов и правок XML)")

    p_ext = sub.add_parser(
        "extension", help="расширения конфигурации: add / sync")
    ext_sub = p_ext.add_subparsers(dest="subcommand", required=True)
    p_ext_add = ext_sub.add_parser(
        "add", help="копия выгрузки расширения из хаба + запись стека + "
        "симлинк source/ext")
    p_ext_add.add_argument("spec", metavar="Имя:Тип:Позиция",
                           help="как у add --extension")
    p_ext_add.add_argument("--config", help="cid (дефолт — активная)")
    p_ext_sync = ext_sub.add_parser(
        "sync", help="сверка каталогов extensions/<cid> со стеком реестра + "
        "симлинк source/ext")
    p_ext_sync.add_argument("--config", help="cid (дефолт — активная)")

    p_help = sub.add_parser(
        "help", help="документация ИТС (корпуса docs/<cid>/<source_id>)")
    help_sub = p_help.add_subparsers(dest="subcommand", required=True)
    p_help_sync = help_sub.add_parser(
        "sync", help="выгрузить/обновить корпус ИТС и проиндексировать в "
        "index.db конфигурации (креды — ITS_* в .env)")
    p_help_sync.add_argument("source_id", help="идентификатор корпуса "
                             "(например ka25doc, ut11doc)")
    p_help_sync.add_argument("--config", help="cid (дефолт — активная)")

    p_status = sub.add_parser(
        "status", help="сводка: дома конфигураций, свежесть исходников "
        "(source_version), стеки расширений, корпуса документации")
    p_status.add_argument("--json", action="store_true",
                          help="ничего не меняет (вывод и так JSON; "
                          "флаг для совместимости со скриптами)")

    p_mig = sub.add_parser(
        "migrate", help="migrate-to-homes: перенос legacy-конфигураций в "
        "дома configs/<cid> (бэкап реестра; контрольная сверка) — S4")
    p_mig.add_argument("--config", help="только эта конфигурация (дефолт — "
                       "все legacy в реестре)")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: 0 — успех, 1 — ошибка (для агентов).

    Неуспех: ключ "error" (полный отказ) или непустой список "errors"
    (частичные ошибки, например у migrate-to-homes).
    """
    args = build_parser().parse_args(argv)
    handlers = {"add": cmd_add, "reindex": cmd_reindex,
                "extension": cmd_extension, "help": cmd_help_sync,
                "status": cmd_status, "migrate": cmd_migrate}
    result = handlers[args.command](args)
    if not isinstance(result, dict):
        return 0
    failed = "error" in result or bool(result.get("errors"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
