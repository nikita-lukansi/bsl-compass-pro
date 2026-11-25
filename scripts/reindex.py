# -*- coding: utf-8 -*-
"""compass-6z4: ручная переиндексация и добавление конфигураций в обход панели.

Панельные сборки крупных конфигураций гибнут по watchdog'у (compass-96j),
ручной отвязанный прогон проходит. Скрипт закрепляет ручной путь как
постоянный инструмент; запуск — ВНУТРИ контейнера:

    python /app/scripts/reindex.py reindex <cid> [--qdrant-stopped]
    python /app/scripts/reindex.py add <alias> --from-dumps <папка_в_/dumps> \\
        [--vectorize --yes-vectorize] [--qdrant-stopped]
    python /app/scripts/reindex.py vectorize <cid> --yes-vectorize \\
        [--wait-warm [сек]]
    python /app/scripts/reindex.py ext-add <Имя> --position <N> \\
        --purpose <Исправление|Адаптация|Дополнение> [--source hub|<путь>]

Конвейер (шаги как у ручного nohup-прогона): gyrfalcon build отвязанным
процессом (start_new_session, stdout/stderr → лог, БЕЗ PIPE) с маркером
EXIT=<код> в логе → поллинг лога (таймаут 90 мин) → fill_bodies →
set_indexed (+ кэш счётчиков, как панель) → прогрев int8/always_ram
коллекции Qdrant → JSON-отчёт в stdout. Лог воркера:
/data/system/logs/reindex_<cid>_<ts>.log (путь — в отчёте).

qdrant — соседний контейнер, docker CLI в образе нет: --stop-qdrant
печатает подсказку и завершается; продолжение — с флагом подтверждения
--qdrant-stopped (владелец остановил qdrant сам). Большие конфигурации
(КА-размер) собираются только при остановленном qdrant — README
«Управление индексами вручную».

add — полный путь новой конфигурации в обход панели: add_config реестра,
копирование выгрузки из /dumps в дом /data/configs/<cid>/source (как при
дом-миграции S4), затем весь конвейер reindex. --vectorize после сборки
запускает отвязанный воркер семантической индексации (программный
эквивалент start_semantic_index); платный внешний API — требует
подтверждения --yes-vectorize, воркер не ждётся (отчёт «started»).

vectorize (compass-8m1) — тот же воркер для УЖЕ собранной конфигурации
(без пересборки индекса); --wait-warm — опциональный поллинг финального
маркера воркера в логе с честным потолком (сам воркер ждём из другого
процесса: его память панели недоступна, межпроцессный финал — JSON-отчёт
main() в логе; реестр vectors по завершении обновляет сам воркер).

ext-add (compass-8m1) — CLI-обёртка агентского тура extension_add
(src/facade/extension_tools.py): регистрация расширения в стеке АКТИВНОЙ
конфигурации + копия источника (hub — из EXT_HUB, путь — каталог).
Пересборку индекса НЕ запускает — честная подсказка reindex <cid>.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Запуск как /app/scripts/reindex.py: путь к src/ — корень /app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import registry as registry_module  # noqa: E402
from src.context import active_config_id, bind_default  # noqa: E402
from src.facade.extension_tools import extension_add  # noqa: E402
from src.recon import bodies as bodies_module  # noqa: E402
from src.workspace import datetime_utc, system_root  # noqa: E402

# Таймаут ожидания воркера сборки (ТЗ 96j: ручной прогон КА — 17-23 мин,
# запас до 90); поллинг лога — раз в 10 с
BUILD_TIMEOUT_S = 90 * 60
POLL_S = 10.0
# Оценка расхода эмбеддингов: карточка метода ≈ 11 токенов (ТЗ владельца)
TOKENS_PER_METHOD = 11
# Потолок --wait-warm для vectorize (по аналогии с wait_warm yho④: честная
# отмена по таймауту, воркер продолжает работать) и интервал поллинга
VECTORIZING_WAIT_TIMEOUT_S = 3600.0
VECTORIZING_POLL_S = 5.0

STOP_QDRANT_HINT = ("выполните на хосте: docker stop bsl-compass-pro-qdrant "
                    "и повторите запуск с --qdrant-stopped (большие "
                    "конфигурации собираются только при остановленном "
                    "qdrant — см. README, «Управление индексами вручную»)")
VECTORIZE_WARNING = ("векторизация ходит в платный внешний API эмбеддингов: "
                     "оценка ≈ <методы> × 11 токенов. Подтвердите флагом "
                     "--yes-vectorize")

# Маркер кода выхода воркера: обёртка sh -c '...; echo EXIT=$?' —
# последняя строка EXIT=<число> в логе
_EXIT_RE = re.compile(r"^EXIT=(\d+)\s*$", re.MULTILINE)
# Финальный маркер воркера векторизации: JSON-отчёт его main() в логе
# (межпроцессный источник готовности — память _sem процесса панели чужому
# скрипту недоступна)
_VECTORIZE_FINAL_RE = re.compile(r'"status":\s*"vectorize_(done|failed)"')


def parse_exit_code(text: str) -> int | None:
    """Код выхода воркера из лога; None — маркера ещё нет (или битый:
    нечисловой EXIT= маркером не считается — воркер не отчитался)."""
    matches = _EXIT_RE.findall(text)
    return int(matches[-1]) if matches else None


def new_log_path(cid: str, prefix: str = "reindex") -> Path:
    """Лог воркера: system/logs/<prefix>_<cid>_<unix-ts>.log (vectorize —
    свой префикс, чтобы логи векторизации были отличимы от сборок)."""
    logs = system_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"{prefix}_{cid}_{int(time.time())}.log"


def spawn_build(source_root: Path, index_db: Path, log_path: Path,
                cid: str) -> subprocess.Popen:
    """Отвязанный воркер сборки (как nohup, compass-96j): sh -c оборачивает
    gyrfalcon build маркером EXIT=$?, stdout/stderr → лог, start_new_session —
    воркер не связан с процессом скрипта. PIPE НЕ используются (панельный
    путь с communicate/PIPE гибнет по watchdog'у). Дескриптор лога родитель
    закрывает сразу — воркер держит свой."""
    index_db.parent.mkdir(parents=True, exist_ok=True)
    inner = (f"gyrfalcon build {shlex.quote(str(source_root))} "
             f"--out {shlex.quote(str(index_db))}; echo EXIT=$?")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"[reindex] {datetime_utc()} cid={cid}\n"
                 f"[reindex] cmd: {inner}\n")
    log_fh = open(log_path, "ab")
    try:
        return subprocess.Popen(["sh", "-c", inner], stdout=log_fh,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
    finally:
        log_fh.close()


def wait_for_exit(log_path: Path, timeout_s: float | None = None,
                   poll_s: float = POLL_S) -> int | None:
    """Поллинг лога до маркера EXIT=<код>; None — таймаут (воркер может
    продолжать работать — отчёт отсылает к логу)."""
    deadline = time.monotonic() + (BUILD_TIMEOUT_S if timeout_s is None
                                   else timeout_s)
    while time.monotonic() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        code = parse_exit_code(text)
        if code is not None:
            return code
        time.sleep(poll_s)
    return None


def warm_qdrant(cid: str) -> dict:
    """Прогрев int8/always_ram коллекции конфигурации (комментарий 96j):
    после старта qdrant холодный поиск бьёт таймаутом — лечится повторным
    enable (disable-формат REST 1.20 не принимает). Логика патча —
    vector_store.set_always_ram (та же, что у panel-warmup fa9).
    «Коллекции нет» (векторизации ещё не было) — не фатально: пометка."""
    try:
        from src.config import config
        from src.semantic.vector_store import VectorStore, set_always_ram
        store = VectorStore(url=config.qdrant_url or None,
                            path="/data/qdrant", on_disk=True)
        result = set_always_ram(store.client,
                                registry_module.collection_name(cid), True)
        # «коллекция недоступна/нет» — честная пометка, не ошибка конвейера
        if "error" in result:
            return {"skipped": result["error"]}
        return result
    except Exception as exc:
        return {"skipped": f"прогрев пропущен: {type(exc).__name__}: {exc} "
                           "(не фатально)"}


def spawn_vectorize_worker(cid: str, log_path: Path) -> None:
    """Отвязанный воркер векторизации: сам скрипт с подкомандой
    _vectorize-worker (программный эквивалент start_semantic_index),
    stdout/stderr → тот же лог. Прогресс — только в логе: панельный
    _sem живёт в другом процессе, кнопки паузы на воркер не действуют."""
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "_vectorize-worker", cid]
    log_fh = open(log_path, "ab")
    try:
        subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    finally:
        log_fh.close()


def _wait_vectorize_final(log_path: Path, timeout_s: float) -> dict:
    """Поллинг лога воркера векторизации до финального маркера (--wait-warm,
    по аналогии с wait_warm из yho④: честный потолок, исключений нет).

    Воркер — отдельный процесс: его _sem/warm_state недоступны, межпроцессный
    финал — JSON-отчёт main() воркера в логе ("vectorize_done|failed").
    Результат: {"result": done|failed|timeout, "waited_s": N} — по
    таймауту воркер продолжает работать, отчёт отсылает к логу."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    t0 = time.monotonic()
    while True:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = _VECTORIZE_FINAL_RE.search(text)
        if m:
            return {"result": ("done" if m.group(1) == "done" else "failed"),
                    "waited_s": round(time.monotonic() - t0, 1)}
        if time.monotonic() >= deadline:
            return {"result": "timeout",
                    "waited_s": round(time.monotonic() - t0, 1),
                    "note": "воркер продолжает работать — смотрите лог"}
        time.sleep(VECTORIZING_POLL_S)


def cmd_vectorize_worker(args) -> tuple[dict, int]:
    """Тело воркера векторизации (внутренняя подкоманда): привязать
    контекст панели к cid, стартовать индексатор, ждать завершения.
    Ждёт без таймаута — воркер отвязанный, ходит часами на больших базах."""
    bind_default(args.cid)
    from src.facade import semantic_tools
    result = semantic_tools.start_semantic_index()
    if result.get("status") != "started":
        return {"status": "vectorize_failed",
                "error": result.get("error") or result.get("info")}, 1
    print(f"[vectorize] started cid={args.cid} "
          f"collection={result.get('collection')}", flush=True)
    while True:
        status = semantic_tools.semantic_status()
        if not status.get("indexing"):
            break
        time.sleep(5)
    stats = status.get("last_stats") or {}
    ok = "error" not in stats
    print(f"[vectorize] finished: {json.dumps(stats, ensure_ascii=False)}",
          flush=True)
    report = ({"status": "vectorize_done", "stats": stats} if ok
              else {"status": "vectorize_failed", "stats": stats})
    return report, (0 if ok else 1)


def _collect_counts(index_db: Path) -> dict | None:
    """Счётчики слоёв индекса для кэша реестра (как панель после сборки):
    статусные эндпоинты и память-гарда читают кэш, не трогая SQLite.
    Сбой — None (set_indexed запишет только время/версию)."""
    try:
        from src.recon.gyr_reader import GyrReader
        rd = GyrReader(index_db)
        try:
            return rd.stats()
        finally:
            rd.close()
    except Exception as exc:
        print(f"[reindex] счётчики не собраны: {type(exc).__name__}: {exc}")
        return None


def run_pipeline(cid: str, *, qdrant_stopped: bool = False,
                 vectorize: bool = False) -> dict:
    """Шаги 2-7 ТЗ после (опционально) регистрации конфигурации: сборка →
    тела → реестр → прогрев → (опц.) векторизация. Итог — JSON-отчёт;
    status: ok | build_failed | timeout | error."""
    t0 = time.monotonic()
    report: dict = {"status": "ok", "steps": []}
    paths = registry_module.build_paths(cid)
    if "error" in paths:
        report.update(status="error", error=paths["error"])
        return report
    report["steps"].append({"step": "paths",
                            "source_root": str(paths["source_root"]),
                            "index_db": str(paths["index_db"])})
    # Симлинк source/ext (S1) — как в панельной сборке, идемпотентен
    link = registry_module.ensure_ext_link(cid)
    if link.get("error"):
        report["steps"].append({"step": "ext_link", "warning": link["error"]})

    log_path = new_log_path(cid)
    spawn_build(paths["source_root"], paths["index_db"], log_path, cid)
    code = wait_for_exit(log_path)
    report["log_path"] = str(log_path)
    if code is None:
        report.update(status="timeout",
                      error=(f"воркер не отчитался за "
                             f"{BUILD_TIMEOUT_S // 60} мин — сборка может "
                             "идти, смотрите лог"))
        return report
    if code != 0:
        report.update(status="build_failed", exit_code=code,
                      error="gyrfalcon build завершился с ошибкой — "
                            "смотрите лог")
        return report
    report["steps"].append({"step": "build", "exit_code": 0})

    # Тела методов в FTS (compass-gbi): сбой не роняет сборку — как панель
    try:
        bodies = bodies_module.fill_bodies(paths["index_db"],
                                           paths["source_root"])
        report["steps"].append({"step": "bodies",
                                **{k: bodies.get(k) for k in
                                   ("status", "bodies", "elapsed_s")}})
    except Exception as exc:
        report["steps"].append({"step": "bodies",
                                "warning": f"{type(exc).__name__}: {exc}"})

    counts = _collect_counts(paths["index_db"])
    registry_module.set_indexed(cid, counts)
    report["steps"].append({"step": "indexed",
                            "methods": (counts or {}).get("methods")})

    if qdrant_stopped:
        report["steps"].append({
            "step": "qdrant_warmup",
            "skipped": "qdrant остановлен владельцем — запустите "
                       "(docker start bsl-compass-pro-qdrant) и прогрейте "
                       "(см. README)"})
    else:
        report["steps"].append({"step": "qdrant_warmup",
                                **warm_qdrant(cid)})

    if vectorize:
        methods = (counts or {}).get("methods") or 0
        spawn_vectorize_worker(cid, log_path)
        report["steps"].append({
            "step": "vectorize", "status": "started", "methods": methods,
            "token_estimate": methods * TOKENS_PER_METHOD,
            "note": "прогресс — в логе (панель этот прогон не показывает)"})

    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    return report


def _resolve_dump(folder: str) -> Path:
    """Путь выгрузки: абсолютный как есть, иначе относительно хаба /dumps
    (env DUMPS_HUB читается на вызов — env-override тестов)."""
    p = Path(folder)
    return p if p.is_absolute() else Path(os.getenv("DUMPS_HUB",
                                                    "/dumps")) / p


def cmd_reindex(args) -> tuple[dict, int]:
    """Подкоманда reindex: --stop-qdrant — только подсказка (docker CLI
    в образе нет), продолжение — с --qdrant-stopped."""
    if args.stop_qdrant and not args.qdrant_stopped:
        return {"status": "stop_qdrant_required", "hint": STOP_QDRANT_HINT}, 2
    report = run_pipeline(args.cid, qdrant_stopped=args.qdrant_stopped)
    return report, (0 if report["status"] == "ok" else 1)


def cmd_add(args) -> tuple[dict, int]:
    """Подкоманда add: регистрация новой конфигурации в обход панели
    (add_config → копирование выгрузки в дом → весь конвейер reindex).
    Порядок как у панели: сначала отказ --vectorize без подтверждения
    (до любых изменений), затем валидация выгрузки, затем реестр."""
    if args.vectorize and not args.yes_vectorize:
        return {"status": "refused",
                "error": "--vectorize требует подтверждения --yes-vectorize",
                "warning": VECTORIZE_WARNING}, 2

    dump = _resolve_dump(args.from_dumps)
    if not dump.is_dir():
        return {"status": "error",
                "error": f"выгрузка не найдена: {dump}"}, 2
    if not (dump / "Configuration.xml").exists():
        return {"status": "error",
                "error": f"в {dump} нет Configuration.xml (не выгрузка "
                         "конфигурации)"}, 2
    for c in registry_module.all_configs():
        if c.get("source_root") == str(dump):
            return {"status": "error",
                    "error": f"эта выгрузка уже зарегистрирована ({c['id']})"}, 2

    # add_config(source_root=None) → дом-схема: entry сразу указывает на
    # /data/configs/<cid>/source (копирование делает вызывающий, S5)
    info = registry_module.read_config_info(dump)
    result = registry_module.add_config(args.alias, None,
                                        display_name=info.get("Name", ""))
    if "error" in result:
        return {"status": "error", "error": result["error"]}, 2
    cid = result["id"]
    source = registry_module.config_source_root(cid)

    # Защита ТЗ: source уже непуст — для него только reindex
    if source.exists() and any(source.iterdir()):
        return {"status": "error", "id": cid,
                "error": f"{source} уже непуст — для такой конфигурации "
                         f"только reindex {cid}"}, 2
    try:
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dump, source, symlinks=True)  # как дом-миграция S4
    except OSError as exc:
        return {"status": "error", "id": cid,
                "error": f"копирование выгрузки не удалось: "
                         f"{type(exc).__name__}: {exc}",
                "hint": f"удалите конфигурацию {cid} и повторите add, либо "
                        f"доложите файлы и запустите reindex {cid}"}, 2

    # Новая конфигурация становится активной — как /api/config/add панели
    bind_default(cid)

    report = run_pipeline(cid, qdrant_stopped=args.qdrant_stopped,
                          vectorize=args.vectorize)
    report.update({"added": cid, "alias": args.alias, "source": str(source)})
    return report, (0 if report["status"] == "ok" else 1)


def cmd_vectorize(args) -> tuple[dict, int]:
    """Подкоманда vectorize (compass-8m1): векторизация УЖЕ собранной
    конфигурации — тот же отвязанный воркер, что у add --vectorize
    (переиспользуется spawn_vectorize_worker). Платный внешний API —
    подтверждение --yes-vectorize обязательно, отказ — ДО старта воркера.
    --wait-warm — поллинг финального маркера воркера в логе (очки points
    по завершении в реестр пишет сам воркер — перечитываем его)."""
    entry = registry_module.get_config(args.cid)
    if entry is None:
        return {"status": "error",
                "error": f"конфигурация '{args.cid}' не найдена в реестре"}, 2
    methods = ((entry.get("index_counts") or {}).get("methods")) or 0
    if not args.yes_vectorize:
        return {"status": "refused",
                "error": "vectorize требует подтверждения --yes-vectorize",
                "warning": VECTORIZE_WARNING.replace("<методы>", str(methods))}, 2

    log_path = new_log_path(args.cid, prefix="vectorize")
    spawn_vectorize_worker(args.cid, log_path)
    report: dict = {
        "status": "started", "cid": args.cid,
        "collection": registry_module.collection_name(args.cid),
        "log_path": str(log_path), "methods": methods,
        "token_estimate": methods * TOKENS_PER_METHOD,
        "note": "прогресс — в логе (панель этот прогон не показывает)"}
    if args.wait_warm is not None:
        report["wait"] = _wait_vectorize_final(log_path, args.wait_warm)
        if report["wait"]["result"] == "done":
            fresh = registry_module.get_config(args.cid) or {}
            vectors = fresh.get("vectors") or {}
            report.update(status="ok", vectorize="done",
                          points=(vectors.get("points") or {}).get("code"),
                          model=vectors.get("model"))
        elif report["wait"]["result"] == "failed":
            report.update(status="vectorize_failed",
                          error="воркер векторизации завершился с ошибкой — "
                                "смотрите лог")
    return report, (1 if report["status"] == "vectorize_failed" else 0)


def cmd_ext_add(args) -> tuple[dict, int]:
    """Подкоманда ext-add (compass-8m1): CLI-обёртка агентского тура
    extension_add (src/facade/extension_tools.py — переиспользуется
    импортом, бизнес-логика регистрации/копии не дублируется). Добавление
    в стек АКТИВНОЙ конфигурации; пересборку индекса НЕ запускает —
    честная подсказка reindex <cid>. Валидация position/purpose — внутри
    extension_add (директива v0.4: hint вместо угадывания)."""
    result = extension_add(name=args.name, position=args.position,
                           purpose=args.purpose, status=args.status,
                           source=args.source)
    if "error" in result:
        return {"status": "error", **result}, 2
    if "hint" in result:
        return {"status": "hint", **result}, 2
    cid = active_config_id() or ""
    # «status» в ответе тура — статус расширения (stable/dev): переносим,
    # верхний статус CLI-отчёта — «ok»
    if "status" in result:
        result["ext_status"] = result.pop("status")
    result.update(status="ok", cid=cid,
                  next=f"индекс не пересобран — выполните reindex {cid}")
    return result, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reindex.py",
        description="Переиндексация/добавление конфигураций, векторизация и "
                    "расширения в обход панели (compass-6z4, compass-8m1); "
                    "запуск внутри контейнера")
    sub = parser.add_subparsers(dest="command", required=True)

    p_re = sub.add_parser("reindex",
                          help="пересобрать индекс существующей конфигурации")
    p_re.add_argument("cid", help="ID конфигурации (Compass_<alias>_<uid>)")
    _add_qdrant_flags(p_re)

    p_add = sub.add_parser(
        "add", help="добавить новую конфигурацию из хаба /dumps и собрать")
    p_add.add_argument("alias", help="короткое имя (латиница/цифры/дефис, "
                                     "1-16 символов)")
    p_add.add_argument("--from-dumps", dest="from_dumps", required=True,
                       help="папка выгрузки в /dumps (или абсолютный путь)")
    p_add.add_argument("--vectorize", action="store_true",
                       help="после сборки запустить векторизацию (платный "
                            "API — требует --yes-vectorize)")
    p_add.add_argument("--yes-vectorize", dest="yes_vectorize",
                       action="store_true",
                       help="подтверждение платной векторизации")
    _add_qdrant_flags(p_add)

    p_vec = sub.add_parser(
        "vectorize", help="векторизация уже собранной конфигурации "
                          "(платный API — требует --yes-vectorize)")
    p_vec.add_argument("cid", help="ID конфигурации (Compass_<alias>_<uid>)")
    p_vec.add_argument("--yes-vectorize", dest="yes_vectorize",
                       action="store_true",
                       help="подтверждение платной векторизации")
    p_vec.add_argument("--wait-warm", dest="wait_warm", type=float,
                       nargs="?", const=VECTORIZING_WAIT_TIMEOUT_S,
                       help=f"после старта ждать финала воркера, сек "
                            f"(без значения — {VECTORIZING_WAIT_TIMEOUT_S:.0f}; "
                            "честный таймаут — воркер продолжает работать)")

    p_ext = sub.add_parser(
        "ext-add", help="добавить расширение в стек активной конфигурации "
                        "(обёртка extension_add; индекс пересобирается "
                        "отдельно — reindex)")
    p_ext.add_argument("name", help="имя расширения (как в конфигураторе)")
    p_ext.add_argument("--position", type=int, required=True,
                       help="позиция в списке «Расширения конфигуратора» "
                            "(1 = верх списка)")
    p_ext.add_argument("--purpose", required=True,
                       help="назначение: Исправление/Patch, "
                            "Адаптация/Customization или Дополнение/Addition")
    p_ext.add_argument("--source", nargs="?", const="", default="",
                       help="источник исходников: hub — выгрузка из EXT_HUB, "
                            "путь — каталог выгрузки; БЕЗ значения — "
                            "исходники уже на месте (копирования нет)")
    p_ext.add_argument("--status", default="stable",
                       choices=("stable", "dev"),
                       help="статус расширения в стеке (по умолчанию stable)")

    p_worker = sub.add_parser("_vectorize-worker", help=argparse.SUPPRESS)
    p_worker.add_argument("cid")
    return parser


def _add_qdrant_flags(parser: argparse.ArgumentParser) -> None:
    """Общие флаги остановки qdrant: --stop-qdrant — запрос (подсказка и
    выход), --qdrant-stopped — подтверждение владельца (продолжить)."""
    parser.add_argument("--stop-qdrant", dest="stop_qdrant",
                        action="store_true",
                        help="напечатать подсказку по остановке qdrant "
                             "и завершиться")
    parser.add_argument("--qdrant-stopped", dest="qdrant_stopped",
                        action="store_true",
                        help="подтверждение: qdrant уже остановлен на хосте")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"reindex": cmd_reindex, "add": cmd_add,
                "vectorize": cmd_vectorize, "ext-add": cmd_ext_add,
                "_vectorize-worker": cmd_vectorize_worker}
    report, code = handlers[args.command](args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
