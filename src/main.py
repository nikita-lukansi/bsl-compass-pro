# -*- coding: utf-8 -*-
"""BSL Compass Pro — ядро-фасад: MCP-тулы разведки поверх Rust-индекса.

Запуск: python -m src.main (или в докере). Транспорт streamable-http
stateless — рестарт контейнера не рвёт сессии клиентов. Пустой контейнер
поднимается в bootstrap-режиме: индекс собирается через панель/визард
(POST /api/bootstrap) или POST /reindex, состояние — /data/config.json.
Здесь — точка сборки: MCP-туры, build-механика (очередь/watchdog/
память-гарда), /health, /api/update-check, /reindex; панельные HTTP-роуты
вынесены в src/panel_api.py (compass-2ao) и регистрируются вызовом
register_panel_routes ниже.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

import uvicorn
from fastmcp import FastMCP

from .config import APP_VERSION, config, git_sha
from .facade import (registry_tools, runtime_tools, semantic_tools, tools,
                     help_tools, extension_tools, dossier)
from .scheduler import RebuildScheduler, interval_minutes, last_build_epoch
from . import backup as backup_engine
from . import update_check
from .notify import notify_build_result
from .facade.repomap import repomap
from . import workspace
from .workspace import init_files
from . import registry as registry_module
from . import session_activity
from . import state
from .context import chat_name_var
from .http_guard import is_local_host_name
from .panel_api import register_panel_routes
from .panel_api import (  # реэкспорты: тесты импортируют из src.main
    _authorized,
    _mutating_guard_response,
    registry_panel_configs as registry_panel_configs,
    upsert_env_keys as upsert_env_keys,
)

logger = logging.getLogger(__name__)


mcp = FastMCP(
    name="bsl-compass-pro",
    instructions="""
    Разведка конфигурации 1С по детерминированному индексу (Rust-движок).
    Протокол: object_overview/repomap — старт; read_function — метод с телом
    и графом; triggers_on_write — что срабатывает при записи;
    verify_field/verify_call — оракулы перед утверждением; who_touches/
    find_references_to — данные и влияние; codesearch — семантический поиск
    (если векторизовали: semantic_index); get_skd_info — схема СКД по пути.
    Рантайм (если подключён RUNTIME_URL): data_query — SELECT к живой базе
(ПЕРВЫЕ N принудительно), get_object — объект по коду/имени/ГУИДу с ТЧ,
    register_records — проводки/записи регистров, catalog_samples — как
заполняются элементы справочников. Только чтение.
Документация продукта (если выгружена: help_status): help_search — поиск
по руководству пользователя, help_read — страница целиком. Протокол:
(1) проектируешь доработку функциональной области — help_search перед
архитектурой; (2) утверждаешь штатное поведение — приведи evidence из
help_read; (3) сомневаешься «штатно или доработка» — сначала найди
настройку в доке. mentions в ответах — имена объектов индекса:
мост «дока ↔ код», дальше verify_field/read_function.
Расширения: extension_stack — карта слоёв; effective_method/who_overrides —
реальный поток с учётом перехватов (вместо чтения только базового кода).
Добавляя расширение (extension_add), ОБЯЗАТЕЛЬНО спроси разработчика:
позиция в списке расширений конфигуратора и назначение (Исправление/
Адаптация/Дополнение) — они задают порядок применения. Порядок «не
подтверждён» — не утверждай последовательность исполнения как факт.
    Ответы с "_no_matches"/"error"/"info" — состояние или честное «нет».
    """,
)


def _tool(fn):
    """Регистрация: результат — JSON-строка (без дублирования structuredContent).

    Синхронные и корутинные тулы (sj2: source_grep в asyncio.to_thread) —
    журнал вызовов общий, логика идентична. P1-1: fastmcp 2.14.7 зовёт
    sync-тул прямо в event loop — offline_wait (до 600 с) замораживал весь
    сервер; sync-ветка стала корутиной, fn зовётся в потоке
    (asyncio.to_thread) — длинный тул больше не блокирует цикл."""
    import functools
    import inspect

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            import time as _time
            from .journal import get_journal as _gj
            t0 = _time.perf_counter()
            ok = True
            result = ""
            try:
                result = json.dumps(await fn(*args, **kwargs),
                                    ensure_ascii=False, default=str)
                return result
            except Exception:
                ok = False
                raise
            finally:
                try:
                    _gj().call(fn.__name__, (_time.perf_counter() - t0) * 1000,
                               ok, args=kwargs,
                               result_size=len(result) if ok else None)
                except Exception:
                    pass  # журнал не ломает тул

        awrapper.__signature__ = inspect.signature(fn)
        return mcp.tool(output_schema=None)(awrapper)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        import time as _time
        from .journal import get_journal as _gj
        t0 = _time.perf_counter()
        ok = True
        result = ""
        try:
            result = json.dumps(await asyncio.to_thread(fn, *args, **kwargs),
                                ensure_ascii=False, default=str)
            return result
        except Exception:
            ok = False
            raise
        finally:
            try:
                _gj().call(fn.__name__, (_time.perf_counter() - t0) * 1000, ok,
                           args=kwargs,
                           result_size=len(result) if ok else None)
            except Exception:
                pass  # журнал не ломает тул

    wrapper.__signature__ = inspect.signature(fn)
    return mcp.tool(output_schema=None)(wrapper)


_tool(tools.index_status)
_tool(tools.search_function)
_tool(tools.read_function)
_tool(tools.read_module)
_tool(tools.list_objects)
_tool(dossier.get_docs)
_tool(dossier.search_dossier)
_tool(dossier.build_docs)
_tool(tools.triggers_on_write)
_tool(tools.who_touches)
_tool(tools.find_references_to)
_tool(tools.find_in_forms)
_tool(tools.object_overview)
_tool(tools.object_rights)
_tool(tools.predefined_items)
_tool(tools.entry_points)
_tool(tools.subsystems_overview)
_tool(tools.subsystems_of)
_tool(tools.verify_field)
_tool(tools.verify_call)
_tool(tools.extension_map)
_tool(registry_tools.config_list)
_tool(registry_tools.config_use)
_tool(tools.get_skd_info)
_tool(repomap)
_tool(semantic_tools.codesearch)
_tool(semantic_tools.start_semantic_index)
_tool(semantic_tools.semantic_status)
_tool(semantic_tools.semantic_mode)
_tool(runtime_tools.runtime_status)
_tool(runtime_tools.data_query)
_tool(runtime_tools.get_object)
_tool(runtime_tools.register_records)
_tool(runtime_tools.catalog_samples)
_tool(runtime_tools.offline_deanonymize)
_tool(runtime_tools.offline_submit)
_tool(runtime_tools.offline_wait)
_tool(runtime_tools.offline_task_status)
_tool(runtime_tools.records)
_tool(runtime_tools.document_movements)
_tool(runtime_tools.event_log)
_tool(tools.predefined_details)
_tool(tools.account_subaccounts)
_tool(help_tools.help_search)
_tool(help_tools.help_read)
_tool(help_tools.help_status)
_tool(help_tools.help_sync)
_tool(help_tools.help_sync_pause)
_tool(help_tools.help_sync_resume)
_tool(help_tools.help_remove)
_tool(help_tools.help_import)
# Т3: механизм расширений (стек, тени, эффективный поток)
_tool(extension_tools.extension_stack)
_tool(extension_tools.extension_add)
_tool(extension_tools.extension_remove)
_tool(extension_tools.extension_move)
_tool(extension_tools.extension_sync_from_ping)
_tool(extension_tools.who_overrides)
_tool(extension_tools.effective_method)
# Т3-Ф5/compass-941.4: инкрементальная пересборка индекса (gyrfalcon update)
_tool(tools.rebuild_incremental)
# Волна 1/compass-20c: промоушены поверх GyrReader (переписаны начисто,
# Community не переносился)
_tool(tools.code_grep)
# compass-d9t (обратная связь №4): grep по телам source_root — вне индекса
_tool(tools.source_grep)
_tool(tools.call_paths)
_tool(tools.who_creates)
_tool(tools.check_claims)
_tool(tools.index_freshness)


# --------------------------------------------------------------- сборка

_build_queue: list[str] = []  # cid, ждущие сборки (FIFO, один писатель; R3)


def _run_build(cid: str, paths: dict) -> None:
    """Фоновая сборка индекса билдером; прогресс — в state.BUILD_STATE.

    Один писатель: следующая сборка из очереди стартует после завершения
    текущей — параллельных gyrfalcon на один индекс-каталог не бывает.
    cid и paths передаются на старте (F2/pu9): пути — из реестра по cid,
    НЕ из контекста (contextvar фоновый поток не наследует).
    """
    gen = state.build_begin()
    # Симлинк source/ext → extensions/<cid> (S1): при индексации
    # расширяемой конфигурации; идемпотентен, legacy-пути не трогает
    link = registry_module.ensure_ext_link(cid)
    if link.get("error"):
        print("[build] симлинк ext:", link["error"])
    tail: list[str] = []
    exit_code = 1
    try:
        paths["index_db"].parent.mkdir(parents=True, exist_ok=True)
        cmd = ["gyrfalcon", "build", str(paths["source_root"]), "--out",
               str(paths["index_db"])]
        # Popen вместо run: PID билдера попадает в BUILD_STATE для
        # watchdog'а (fc9) — building=True при мёртвом процессе дольше
        # BUILD_WATCHDOG_MIN минут сбрасывается принудительно
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
        # compass-2xo: communicate вернул управление — воркер завершился
        # (сам или по нашему kill-таймауту), дальше пост-шаги. Фаза "post"
        # в build-блоке отличает это от смерти воркера: watchdog не пишет
        # ложный exit=-1, пока fill_bodies/досье идут (инцидент 96j —
        # ложный −1 висел ~20 мин поверх успешного прогона)
        state.build_phase_post(exit_code)
        notify_build_result(paths["display_name"], exit_code, tail)
        # Кэш счётчиков: stats() по 653k строк через drvfs — минуты;
        # считаем ОДИН раз здесь (фоновый поток) и пишем в реестр —
        # статусные эндпоинты читают кэш, не трогая SQLite
        if exit_code == 0:
            try:
                from .facade.tools import reader, reset_reader
                reset_reader()
                counts = reader().stats()
                registry_module.set_indexed(cid, counts)
            except Exception as exc:
                print("[build] кэш счётчиков не записан:", exc)
            # Тела методов в FTS (compass-gbi): пост-шаг нашей сборки поверх
            # чужого билдера; сбой не роняет сборку — code_grep честно
            # отдаёт name-only маршрут, тела появятся при следующей сборке
            try:
                from .recon.bodies import fill_bodies
                report = fill_bodies(paths["index_db"], paths["source_root"])
                print("[build] тела методов:", report.get("status"),
                      report.get("bodies"), "шт,",
                      report.get("elapsed_s"), "с")
            except Exception as exc:
                print("[build] тела методов не залиты:", exc)
        # Каркас досье (Т4): регенерация из свежего индекса; сбой не роняет сборку
        try:
            from .facade.dossier import build_docs as _build_docs
            report = _build_docs(cid)
            print("[build] досье:", report.get("written")
                  or report.get("error"))
        except Exception as exc:
            print("[build] досье не собрано:", exc)
        # Триггер автобэкапа (концепция #30): свежий слепок сразу после сборки
        if exit_code == 0 and os.getenv("BACKUP_AUTO", "off").lower() == "on":
            try:
                backup_engine.create_backup(cid, paths["display_name"],
                                            paths["index_db"])
            except Exception as exc:
                print("[backup] авто-бэкап не удался:", exc)
    except Exception as exc:
        # Любое падение потока сборки (mkdir, запуск билдера) обязано
        # попадать в состояние — иначе building=True зависает навсегда
        # и блокирует новые сборки (инцидент КА 2026-11-05).
        tail = [f"{type(exc).__name__}: {exc}"]
        print("[build] ошибка потока сборки:", tail[0])
    finally:
        # gen: stale-finish очнувшегося потока (после watchdog-сброса
        # зависшей сборки) состояние новой сборки не перезапишет
        state.build_finish(exit_code, tail, gen)
        _drain_build_queue()


def _drain_build_queue() -> None:
    """Взять следующую конфигурацию из очереди (если есть) и собрать.

    Переключение — реестровым bind контекста (R3/A20), не state.use_config.
    """
    from .context import bind_default, bind_context, PANEL_CONTEXT
    while _build_queue:
        cid = _build_queue.pop(0)
        workspace.save_build_queue(_build_queue)  # персистентность (R15/A16)
        if not registry_module.get_config(cid):
            continue  # конфигурация удалена из реестра — пропускаем
        bind_default(cid)
        bind_context(cid, PANEL_CONTEXT)
        registry_tools.reset_runtime_caches()
        ok, msg = _start_build_locked()
        if ok:
            return
        print("[build] очередь:", msg)  # отказ (напр. память-гарда) — в лог


def _build_paths(cid: str) -> dict:
    """Пути сборки по реестру (F2/pu9) — логика в registry.build_paths."""
    return registry_module.build_paths(cid)


# Диагноз compass-08q: воркер полной сборки крупной конфигурации (~3,5 ГиБ
# пик) + qdrant (~2 ГиБ) не помещаются в лимит памяти ВМ Docker Desktop —
# сборка умирает бесшумно (OOM уровня ВМ, cgroup-kill не срабатывает).
# Гард перед полной сборкой: малый запас при крупном исходнике — честный
# отказ с подсказкой. НЕ автоматизирует stop qdrant (решение владельца,
# см. README «Обновление»).
_BUILD_MEM_RESERVE = int(4.5 * 1024**3)  # запас для воркера крупного исходника
_LARGE_METHODS = 400_000  # «крупный»: кэш счётчиков реестра, methods


def cgroup_available_bytes(root: str = "/sys/fs/cgroup") -> int | None:
    """Свободная память контейнера (cgroup v2): memory.max − memory.current.

    None — оценить нельзя: cgroup-файлов нет (хост-запуск/тесты) или лимит
    не задан (memory.max = «max») — гард в этих случаях не срабатывает.
    """
    try:
        max_text = (Path(root) / "memory.max").read_text(
            encoding="ascii").strip()
        current = int((Path(root) / "memory.current").read_text(
            encoding="ascii").strip())
        if max_text == "max":
            return None
        return int(max_text) - current
    except (OSError, ValueError):
        return None


def _memory_gate(cid: str) -> str | None:
    """Отказ-сообщение, если полной сборке не хватит памяти; None — пускать.

    «Крупный» исходник — по кэшу счётчиков реестра (methods > 400k,
    пишется после каждой сборки); у ПЕРВОЙ сборки кэша нет — гард её не
    оценивает (ручные прогоны — по правилу README «Обновление»).
    """
    entry = registry_module.get_config(cid) or {}
    methods = ((entry.get("index_counts") or {}).get("methods")) or 0
    if methods <= _LARGE_METHODS:
        return None
    available = cgroup_available_bytes()
    if available is None or available >= _BUILD_MEM_RESERVE:
        return None
    return (f"память контейнера: свободно ~{available / 1024**3:.1f} ГиБ, "
            f"воркеру крупной конфигурации ({methods} methods) нужен запас "
            f"~4,5 ГиБ — остановите bsl-compass-pro-qdrant и повторите "
            "(см. README, раздел «Обновление»)")


def _start_build_locked(enqueue_if_busy: bool = True) -> tuple[bool, str]:
    """Единый запуск сборки с защитой от наложения (одна одновременно).

    Занято → конфигурация встаёт в FIFO-очередь и соберётся следующей.
    Память-гарда (compass-756): крупный исходник при малом свободном
    запасе cgroup — отказ с подсказкой, сборка не стартует.
    """
    if state.BUILD_STATE["building"]:
        if enqueue_if_busy:
            cid = config.active_config_id
            if cid and cid not in _build_queue:
                _build_queue.append(cid)
                workspace.save_build_queue(_build_queue)  # R15/A16
                return False, "поставлено в очередь (сборка занята)"
        return False, "сборка уже идёт"
    if help_tools._docs["fetching"]:
        # kb sync пишет help-страницы в тот же index.db — сборка подождёт;
        # в очередь сборку не ставим: прогон доки обычно минуты
        return False, "идёт синхронизация документации — повторите после неё"
    cid = config.active_config_id  # фиксируем ДО старта потока (contextvar!)
    paths = _build_paths(cid)
    if "error" in paths:
        return False, paths["error"]
    refusal = _memory_gate(cid)
    if refusal:
        return False, refusal
    threading.Thread(target=_run_build, args=(cid, paths), daemon=True,
                     name="index-build").start()
    return True, "started"


# --------------------------------------------------------------- HTTP-роуты

@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse

    out = {
        "status": "healthy",
        "version": APP_VERSION,
        "git_sha": git_sha(),
        "index": "ok" if config.index_ready else "not_configured",
    }
    if not config.admin_token:
        # аудит 4l5 P1-1: панель и owner-API открыты — честно предупреждаем
        out["warn"] = "admin_token not set"
    return JSONResponse(out)


# Файл переменных для compose (маунт ./.env:/env/.env:rw); COMPASS_ENV_FILE —
# override для хост-запуска/тестов. Роут /api/settings/hubs (запись ключей
# хабов) — в panel_api.py, файл получает accessor'ом env_file
ENV_FILE = Path(os.getenv("COMPASS_ENV_FILE", "/env/.env"))
# Панельные HTTP-роуты (compass-2ao): обработчики — src/panel_api.py,
# регистрация здесь (точка сборки). deps передаются явно: сборка/очередь —
# build-механика этого модуля; env_file — accessor к ENV_FILE (тесты
# патчуют src.main.ENV_FILE).
register_panel_routes(
    mcp,
    start_build=_start_build_locked,
    build_queue=_build_queue,
    env_file=lambda: ENV_FILE,
)


@mcp.custom_route("/api/update-check", methods=["GET"])
async def api_update_check(request):
    """Ручная перепроверка актуальности версии (compass-82a): свежий запрос
    к GitHub main + обновление кэша (для панели/агента). Сеть — в поток,
    event loop не блокируется; UPDATE_CHECK=off — честный off-блок без сети."""
    from starlette.responses import JSONResponse

    if not update_check.enabled():
        return JSONResponse(update_check.update_status())
    return JSONResponse(await asyncio.to_thread(update_check.check_update))


@mcp.custom_route("/reindex", methods=["POST"])
async def reindex(request):
    """Пересборка индекса активной выгрузки (env или state)."""
    from starlette.responses import JSONResponse

    # аудит 4l5 P1-1: мутирующий POST — тот же Origin/Host/Content-Type
    # гейт, что и у панельных роутов (src/panel_api.py)
    denial = _mutating_guard_response(request)
    if denial is not None:
        return denial
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ok, msg = _start_build_locked()
    return JSONResponse({"status": msg}, status_code=200 if ok else 409)


class HostGateMiddleware:
    """Гейт Host-заголовка для ВСЕХ запросов (публикация F / compass-rjq).

    Аудит В3: Origin/Host-гейт security-волны 2 висел только на панельных
    POST — GET owner-виды (/api/journal, /api/offline/tasks) и весь /mcp
    обслуживались DNS-rebinding-странице с чужим Host (живьём: 200).
    Оборачивает приложение целиком (панель, /mcp, /health, ассеты):
    не-localhost Host → 403; пустой/отсутствующий Host (HTTP/1.0,
    не-браузерные клиенты) — пропуск. POST-специфичные проверки
    (Origin + Content-Type) остаются в панельных роутах (panel_api).

    Слой ВНЕ ChatNameMiddleware: rebinding-пробы не тикают активность
    чата (session_activity) и не поднимают contextvar.
    """

    _DENY_BODY = b'{"error": "host not allowed"}'

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = ""
            for key, value in scope.get("headers", []):
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            if host and not is_local_host_name(host):
                await send({
                    "type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(self._DENY_BODY)).encode("ascii"))],
                })
                await send({"type": "http.response.body",
                            "body": self._DENY_BODY})
                return
        await self.app(scope, receive, send)


class ChatNameMiddleware:
    """X-Chat-Name из заголовка → contextvar: тулы читают активную
    конфигурацию СВОЕГО чата. Панель/REST без заголовка — контекст panel.

    Попутно отмечает активность чата (session_activity.touch) — только
    для значимых запросов (mcp/api/reindex), статика /ui,/assets,/health
    счётчик не раздувает.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            chat = (headers.get("x-chat-name") or "").strip()[:100]
            chat_name_var.set(chat or "panel")
            path = scope.get("path", "")
            # /api/status — poll панели: счётчик активности не раздуваем
            # (R14/A15); mcp и прочие api тикают
            if (path.startswith(("/mcp", "/api", "/reindex"))
                    and path != "/api/status"):
                session_activity.touch(chat or "panel")
        await self.app(scope, receive, send)


def _migrate_legacy_configs() -> None:
    """Разовая миграция /data/config.json (алиасы) → новый реестр
    (логика в registry.migrate_legacy_configs — R12/A23)."""
    registry_module.migrate_legacy_configs()


def main() -> None:
    if not config.admin_token:
        # аудит 4l5 P1-1: обязательность ADMIN_TOKEN — решение владельца;
        # пока токен не задан — одно громкое предупреждение при старте
        # (+ строка warn в /health)
        print("=" * 66)
        print(" ВНИМАНИЕ: ADMIN_TOKEN не задан — панель и owner-API открыты")
        print(" без аутентификации. Задайте ADMIN_TOKEN в .env и перезапустите.")
        print("=" * 66)
    state.load_state()
    init_files()
    session_activity.restore()
    _migrate_legacy_configs()
    # Миграция испорченных привязок (F1/zjp): config_id='panel' и мусорные
    # cid из-за инверсии bind_context до фикса — чат терял индекс
    registry_module.normalize_session_bindings()
    # Восстановление очереди сборки (R15/A16): только cid, живые в реестре
    _build_queue[:] = [cid for cid in workspace.load_build_queue()
                       if registry_module.get_config(cid)]
    # Прогрев кэша звёзд GitHub (R6/A7): разогрет до первого запроса панели
    from . import links as _links
    threading.Thread(target=_links.github_info, daemon=True,
                     name="links-warmup").start()
    if os.getenv("RUNTIME_MODE", "online").lower() == "offline":
        # Реестр офлайн-задач: старт watcher + восстановление из файлов
        # обмена при старте ядра, а не лениво из горячих статусов
        # (P43-примечание)
        from .runtime.tasks import get_registry
        get_registry()
    if interval_minutes() > 0:
        RebuildScheduler(
            build_callback=lambda: _start_build_locked()[0],
            get_last_build_ts=last_build_epoch,
        ).start()
    # Watchdog зависшей сборки (fc9): отдельный поток — автосборка может
    # быть выключена, а building=True без живого gyrfalcon ломает панель
    state.start_build_watchdog()
    # Фоновая проверка актуальности версии (compass-82a): daemon-поток,
    # первый чек через ~30 с, далее раз в 900 с; UPDATE_CHECK=off — мимо
    update_check.start_background()
    # Стек middleware: Host-гейт внешним слоем (публикация F/compass-rjq —
    # DNS-rebinding отсекается до тика активности чата), затем имя чата
    app = HostGateMiddleware(
        ChatNameMiddleware(mcp.http_app(stateless_http=True)))
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level="info")


if __name__ == "__main__":
    main()
