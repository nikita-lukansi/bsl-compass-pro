# -*- coding: utf-8 -*-
"""Панельные HTTP-эндпоинты BSL Compass Pro (compass-2ao: вынос из main.py).

Все веб-панельные роуты — здесь: UI-статика панели, /api/status, офлайн-
конвейер, реестр конфигураций и стек расширений, бэкапы, настройки
(хабы/эмбеддинг), docs/semantic-управление, ссылки/версия. Регистрация —
register_panel_routes(mcp, ...), вызывается из main.py (точка сборки).
Поведение идентично прежнему: пути, методы, форматы и коды ответов не
менялись (чистый рефакторинг).

Зависимости от main.py передаются явно через register_panel_routes
(без цикла импортов и без новых глобалов сверх необходимого):
start_build — запуск сборки (main._start_build_locked);
build_queue — FIFO-очередь сборок (список main._build_queue,
мутируется на месте, читается по ссылке);
env_file — accessor → Path .env-файла compose (main.ENV_FILE; именно
callable, а не значение: тесты патчуют атрибут src.main.ENV_FILE).

Здесь же гейт _authorized (timing-safe токен) и хелперы
registry_panel_configs/upsert_env_keys — main.py реэкспортирует их для
совместимости импортов тестов. MCP-туры, /health, /api/update-check,
/reindex и build-механика (watchdog, очередь, память-гарда) — в main.py.
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import backup as backup_engine
from . import registry as registry_module
from . import session_activity
from . import state
from . import update_check
from . import workspace
from .config import APP_VERSION, config, git_sha
from .facade import (extension_tools, help_tools, registry_tools,
                     runtime_tools, semantic_tools, tools)
from .http_guard import (is_local_host_name as _is_local_host_name,
                         safe_static_path, validated_name)
from .journal import get_journal
from .panel_page import PANEL_HTML
from .scheduler import interval_minutes

# task_id в resp-файле (роут ручной загрузки): безопасное имя, без путей
_RESP_TASK_ID_RE = re.compile(r"[0-9a-z_]{6,32}")

# deps от main.py — проставляются register_panel_routes (точка сборки):
# явная передача состояния вместо импорта main (разрыв цикла импортов)
_deps: dict = {}


def _authorized(request) -> bool:
    """Гейт мутирующих и owner-эндпоинтов: ADMIN_TOKEN не задан — доступ
    свободен; сравнение timing-safe (аудит SAFE-6)."""
    token = config.admin_token
    if not token:
        return True
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""
    if bearer and hmac.compare_digest(bearer, token):
        return True
    header_token = request.headers.get("x-admin-token", "")
    return bool(header_token) and hmac.compare_digest(header_token, token)


# _is_local_host_name — в src/http_guard.py (публикация F/compass-rjq:
# общий хелпер для панельного POST-гейта и глобального HostGateMiddleware)


def _mutating_guard_response(request):
    """Гейт мутирующих POST (аудит 4l5 P1-1): Origin/Host + Content-Type.

    Параметры:
        request — starlette Request.

    Возвращаемое значение:
        JSONResponse-отказ (403 чужой Origin/Host — CSRF и DNS-rebinding;
        415 POST с телом без Content-Type: application/json — режет
        cross-site text/plain) или None — гейт пройден. Отсутствующий
        Origin/Host (не-браузерные клиенты) не блокируется.
    """
    from starlette.responses import JSONResponse

    origin = request.headers.get("origin", "")
    if origin and not _is_local_host_name(origin):
        return JSONResponse({"error": "origin forbidden"}, status_code=403)
    host = request.headers.get("host", "")
    if host and not _is_local_host_name(host):
        return JSONResponse({"error": "host forbidden"}, status_code=403)
    if request.method == "POST":
        has_body = (request.headers.get("transfer-encoding", "") != ""
                    or request.headers.get("content-length", "") not in ("", "0"))
        media = request.headers.get("content-type", "").split(";", 1)[0]
        if has_body and media.strip().lower() != "application/json":
            return JSONResponse(
                {"error": "POST body requires Content-Type: application/json"},
                status_code=415)
    return None


def _guarded(handler):
    """Обёртка мутирующих роутов панели: P1-1-гейт до хендлера.

    Параметры:
        handler — async-хендлер роута (принимает request).

    Возвращаемое значение:
        async-хендлер той же сигнатуры: сначала Origin/Host/Content-Type
        гейт, при отказе — готовой 403/415, иначе — исходный handler.
    """
    @functools.wraps(handler)
    async def guarded_handler(request):
        denial = _mutating_guard_response(request)
        if denial is not None:
            return denial
        return await handler(request)
    return guarded_handler


def registry_panel_configs() -> list[dict]:
    """Конфигурации реестра для панели: + points.code, + active контекста."""
    from .context import active_config_id
    from .registry import all_configs

    active_id = active_config_id()
    out = []
    for c in all_configs():
        vectors = c.get("vectors") or {}
        points = (vectors.get("points") or {}).get("code", 0)
        out.append({
            "id": c["id"],
            "alias": c["id"],  # совместимость с текущей панелью
            # index в volume-домах: дом-схема S4 (/data/configs/<cid>/)
            # и легаси-модель 06k (/data/index/) — оба в volume compass-data
            "index_in_volume": (c.get("index_db") or "").startswith(
                ("/data/configs/", "/data/index/")),
            "short_name": c.get("short_name", ""),
            "display_name": c.get("display_name", ""),
            "source_root": c.get("source_root", ""),
            "index_db": c.get("index_db", ""),
            "index_ready": Path(c.get("index_db", "")).exists(),
            "active": c["id"] == active_id,
            "mode": c.get("mode", "fast"),
            "vectors": points,
            "config_info": c.get("config_info", {}),
            "indexed_at": c.get("indexed_at"),
            "source_version": c.get("source_version"),
        })
    return out


async def panel_ui(request):
    """Веб-панель: Vue 3 SPA (panel/dist/index.html)."""
    from starlette.responses import FileResponse

    dist = Path(__file__).parent.parent / "panel" / "dist" / "index.html"
    if not dist.exists():
        # fallback на embedded HTML (если panel не собрана)
        from starlette.responses import HTMLResponse
        return HTMLResponse(PANEL_HTML)
    return FileResponse(dist, media_type="text/html")


async def panel_assets(request):
    """Статика Vue SPA (js/css из panel/dist/assets/); путь проверен на
    принадлежность каталогу (аудит SAFE-1: path traversal)."""
    from starlette.responses import FileResponse, JSONResponse

    base = Path(__file__).parent.parent / "panel" / "dist" / "assets"
    asset = safe_static_path(base, request.path_params.get("path", ""))
    if asset is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    media = "application/javascript" if asset.suffix == ".js" else "text/css"
    return FileResponse(asset, media_type=media)


async def panel_logo(request):
    """Логотип BSL Compass (шапка панели и иконка вкладки)."""
    from starlette.responses import FileResponse

    return FileResponse(
        Path(__file__).parent / "static" / "compass_logo.jpg",
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def api_status(request):
    """Сводка для панели: индекс, сборка, семантика, доступные выгрузки."""
    from starlette.responses import JSONResponse

    from .runtime.tasks import offline_summary as tasks_offline_summary
    journal = get_journal()
    out = {
        "version": APP_VERSION,
        "git_sha": git_sha(),
        "alias": config.alias,
        # аудит 4l5 P2-2: только счётчики; recent (owner-вид) — за
        # токеном в GET /api/journal
        "journal": journal.stats_24h(),
        # P1-2: scan_dumps — обход каталогов выгрузок (диск) — в поток
        "dumps": await asyncio.to_thread(state.scan_dumps),
        # compass-85s/bcl: каталоги входных хабов для «Настройки → Каталоги
        # выгрузок» — фактические host-пути из .env (compose пробрасывает их
        # в environment; ядро сканирует по маунт-пойнтам — guard в state.py)
        "hubs": {
            "dumps_hub": os.getenv("DUMPS_HUB") or "./dumps",
            "ext_hub": os.getenv("EXT_HUB") or "./ext-dumps",
            "docs_hub": os.getenv("DOCS_HUB") or "./docs-dumps",
        },
        "build": dict(state.BUILD_STATE),
        # аудит 4l5 P2-3: сбор статуса семантики уходит в поток — при
        # остановленном qdrant (подъём стека с таймаутом) /api/status не
        # блокирует event loop; ответ честный, с unavailable-статусом
        "semantic": await asyncio.to_thread(semantic_tools.semantic_status),
        # P1-2: help_status читает help-таблицы из index.db (SQLite) — в поток
        "docs": await asyncio.to_thread(help_tools.help_status),
        # P43: лёгкий статус без сети — ping с таймаутом 120 с блокировал
        # event loop на каждом опросе панели; полный статус — MCP-тул
        # runtime_status (идёт в пуле потоков) или кнопка в панели
        "runtime": runtime_tools._runtime_brief(),
        # Т2: сводка офлайн-конвейера (реестр стартует в main() в offline);
        # offline_summary не создаёт реестр — в online он и не нужен;
        # P1-2: список задач под локом watcher'а — IO в поток
        "offline": (await asyncio.to_thread(tasks_offline_summary)
                    if os.getenv("RUNTIME_MODE", "online")
                    .lower() == "offline" else None),
        # compass-82a: актуальность версии контура vs GitHub main — блок
        # из кэша фонового потока (сети здесь нет; ручная перепроверка —
        # GET /api/update-check)
        "update": update_check.update_status(),
        "schedule_min": interval_minutes(),
        # P1-2: Path.exists по каждому индексу реестра (диск) — в поток
        "configs": await asyncio.to_thread(registry_panel_configs),
        "sessions": session_activity.sessions_panel_list(),
        "build_queue": list(_deps["build_queue"]),
    }
    if config.index_ready and not state.BUILD_STATE["building"]:
        # Счётчики — из кэша реестра (пишется при сборке): живой stats()
        # через drvfs занимает минуты и вешает панель (инцидент 2026-11-05)
        try:
            from .registry import get_config
            entry = get_config(config.active_config_id) or {}
            cached = entry.get("index_counts") or {}
            out["index"] = {"status": "ok",
                            "counts": cached or None,
                            "counts_note": "кэш на момент сборки" if cached else
                                           "счётчики появятся после пересборки"}
        except Exception as exc:
            out["index"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        out["index"] = {"status": "building" if state.BUILD_STATE["building"]
                        else "not_configured"}
    return JSONResponse(out)


async def api_journal(request):
    """Последние вызовы тулов — owner-вид за токеном (аудит 4l5 P2-2):
    /api/status отдаёт только счётчики журнала, tail — здесь.

    ?limit= обрезается до 1..200; дефолт 10 — как было в /api/status
    до выноса за токен.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "10"))))
    except ValueError:
        limit = 10
    return JSONResponse({"recent": get_journal().tail(limit=limit)})


# --------------------------------------------------- офлайн-конвейер (Т2)

async def api_offline_tasks(request):
    """Панель «Живые данные»: список задач реестра, owner-вид (панель —
    владелец: все чаты), без params вызовов. Фильтры ?status= &chat=."""
    from starlette.responses import JSONResponse

    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "tasks": get_registry().list(
            chat=request.query_params.get("chat") or None,
            status=request.query_params.get("status") or None,
            owner=True),
    })


async def api_offline_task(request):
    """Полный owner-вид задачи: вызовы (методы), переходы, результаты."""
    from starlette.responses import JSONResponse

    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    view = get_registry().task_view("panel", task_id, owner=True)
    if view is None:
        return JSONResponse({"error": f"задача {task_id} не найдена"},
                            status_code=404)
    return JSONResponse(view)


async def api_offline_task_cancel(request):
    """Отмена задачи (панель — владелец: любой чат; только queued)."""
    from starlette.responses import JSONResponse

    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = get_registry().cancel("panel", request.path_params.get("task_id", ""),
                                owner=True)
    return JSONResponse(out, status_code=200 if "error" not in out else 409)


async def api_offline_task_resubmit(request):
    """Переотправка: новый submit с теми же calls/description (новый
    task_id), старая задача → cancelled. Ответ: {"resubmitted": <new_id>}."""
    from starlette.responses import JSONResponse

    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = get_registry().resubmit("panel",
                                  request.path_params.get("task_id", ""),
                                  owner=True)
    return JSONResponse(out, status_code=200 if "error" not in out else 409)


async def api_offline_request_download(request):
    """Скачивание req-файла из queue/ (фолбэк без прокинутого диска).

    Защита от path-traversal: имя строго req_<id>.[json|taken|done]."""
    from starlette.responses import FileResponse, JSONResponse

    from .runtime.tasks import get_registry, safe_queue_name
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.path_params.get("name", "")
    if not safe_queue_name(name):
        return JSONResponse({"error": "недопустимое имя файла"}, status_code=400)
    path = get_registry().root / "queue" / name
    if not path.exists():
        return JSONResponse({"error": "файл не найден"}, status_code=404)
    return FileResponse(path, media_type="application/json", filename=name)


async def api_offline_response_upload(request):
    """Ручная загрузка resp (панель прочитала файл и прислала содержимое —
    фолбэка без прокинутого диска): ядро парсит, берёт task_id, атомарно
    пишет results/resp_<task_id>.json — дальше обычный тик watcher'а."""
    from starlette.responses import JSONResponse

    from .runtime import offline
    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body: сырой JSON ответа 1С"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body: сырой JSON ответа 1С"},
                            status_code=400)
    task_id = str(body.get("task_id") or "").strip()
    if not _RESP_TASK_ID_RE.fullmatch(task_id):
        return JSONResponse({"error": "task_id обязателен (resp-формат)"},
                            status_code=400)
    reg = get_registry()
    offline.write_atomic(reg.root / "results" / f"resp_{task_id}.json",
                         json.dumps(body, ensure_ascii=False))
    return JSONResponse({"queued": f"resp_{task_id}.json",
                         "hint": "watcher заберёт ответ в обычном тике"})


async def api_offline_health(request):
    """Проба каталога обмена (аудит 4l5 P3-1).

    HEAD — без токена и БЕЗ записи: лёгкий health-check (200 = жив);
    GET — owner-проба записи (создать+удалить пробный файл), за
    _authorized (панель шлёт токен с каждым запросом).
    """
    import uuid

    from starlette.responses import JSONResponse, Response

    if request.method == "HEAD":
        return Response(status_code=200)
    from .runtime import offline
    from .runtime.tasks import get_registry
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    reg = get_registry()
    offline.ensure_dirs(reg.root)
    probe = reg.root / "queue" / f"health_probe_{uuid.uuid4().hex[:8]}.tmp"
    writable = True
    try:
        probe.write_text("{}", encoding="utf-8")
        probe.unlink()
    except OSError:
        writable = False
    return JSONResponse({"writable": writable, "root": str(reg.root)})


async def api_bootstrap(request):
    """Визард: выбрать выгрузку из /dumps и собрать индекс (пишет реестр).

    Единый реестровый путь добавления (A2/P10): short_name выводится из
    имени каталога выгрузки; при нарушении правил ([A-Za-z0-9_-]{1,16}) —
    честный отказ с подсказкой /api/config/add.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    source_root = request.query_params.get("source_root", "")
    if not source_root:
        return JSONResponse({"error": "source_root обязателен"}, status_code=400)
    for c in registry_module.all_configs():
        if c.get("source_root") == source_root:
            return JSONResponse(
                {"error": f"эта выгрузка уже зарегистрирована ({c['id']})"},
                status_code=400)
    short_name = Path(source_root.rstrip("/")).name
    # P1-2: add_config обходит дерево выгрузки — в поток
    result = await asyncio.to_thread(registry_module.add_config,
                                     short_name, source_root)
    if "error" in result:
        return JSONResponse(
            {"error": (result["error"] + "; используйте /api/config/add"
                       " с явным short_name")},
            status_code=400)
    cid = result["id"]
    # привязать к контексту панели, как в /api/config/add (R3/A20)
    from .context import bind_default, bind_context, PANEL_CONTEXT
    bind_default(cid)
    bind_context(cid, PANEL_CONTEXT)
    registry_tools.reset_runtime_caches()
    ok, msg = _deps["start_build"]()
    return JSONResponse({"status": msg, "id": cid, "alias": short_name},
                        status_code=200 if ok or "очеред" in msg else 409)


async def api_backups(request):
    """Слепки активной конфигурации (для экрана панели)."""
    from starlette.responses import JSONResponse

    raw_alias = request.query_params.get("alias")
    if raw_alias is not None and validated_name(raw_alias) is None:
        return JSONResponse({"error": "недопустимый alias"}, status_code=400)
    alias = raw_alias or config.alias
    root = backup_engine.BACKUPS_HUB / alias
    items = []
    if root.is_dir():
        for child in sorted(root.iterdir(), reverse=True):
            mf = child / "manifest.json"
            manifest = {}
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            items.append({"name": child.name,
                          "created_at": manifest.get("created_at"),
                          "counts": manifest.get("index_counts")})
    # hub — фактический каталог хранилища: панель показывает, куда пишет движок
    return JSONResponse({"alias": alias, "backups": items,
                         "hub": str(backup_engine.BACKUPS_HUB),
                         "inbox_hub": str(backup_engine.INBOX_HUB),
                         "keep": backup_engine.BACKUP_KEEP})


async def api_backup_create(request):
    """Создать слепок сейчас (VACUUM INTO + снапшот Qdrant + манифест)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not config.index_ready:
        return JSONResponse({"error": "индекс не собран"}, status_code=409)
    # P1-2: VACUUM INTO + снапшот Qdrant — секунды-минуты диска, в поток
    return JSONResponse(await asyncio.to_thread(
        backup_engine.create_backup, config.active_config_id, config.alias,
        config.index_db))


async def api_backup_restore_semantic(request):
    """Восстановить семантику из слепка: снапшот → Qdrant (F7/vlr).
    Body: {id: cid, dir: "<alias>_<stamp>"} — целевая коллекция —
    иммутабельное имя реестра; сверка с манифестом слепка.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    dir_name = str(body.get("dir") or "").strip()
    if not cid or not dir_name:
        return JSONResponse({"error": "id и dir обязательны"},
                            status_code=400)
    report = await asyncio.to_thread(backup_engine.restore_semantic,
                                     cid, dir_name)  # P1-2: снапшот → Qdrant
    code = 409 if "error" in report else 200
    return JSONResponse(report, status_code=code)


async def api_inbox(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"inbox": backup_engine.scan_inbox()})


async def api_inbox_apply(request):
    """Восстановить принесённый слепок из /inbox (с переносом в архив)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.query_params.get("dir", "")
    if validated_name(name) is None:
        return JSONResponse({"error": "недопустимый dir"}, status_code=400)
    # P1-2: перенос слепка в архив — тяжёлый дисковый IO, в поток
    result = await asyncio.to_thread(backup_engine.apply_inbox_backup, name)
    code = 200 if "restored" in result else 409
    return JSONResponse(result, status_code=code)


async def api_backup_restore(request):
    """Восстановить слепок из архива /backups (деструктивно, за токеном)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.query_params.get("dir", "")
    raw_alias = request.query_params.get("alias")
    if validated_name(name) is None or (
            raw_alias is not None and validated_name(raw_alias) is None):
        return JSONResponse({"error": "недопустимые dir/alias"},
                            status_code=400)
    alias = raw_alias or config.alias
    source = backup_engine.BACKUPS_HUB / alias / name
    # P1-2: деструктивное восстановление = массовый дисковый IO, в поток
    result = await asyncio.to_thread(backup_engine.restore_backup, source)
    code = 200 if "restored" in result else 409
    return JSONResponse(result, status_code=code)


async def api_dumps_available(request):
    """Скан папок-выгрузок для окна «Добавить» (R7/A9): оба хаба —
    workspace/Configurations и DUMPS_HUB, дедуп по source_root,
    поле hub указывает источник. Логика — state.scan_available_dumps.
    compass-85s: плюс списки папок хабов расширений (ext) и готовых
    док-корпусов (docs) — state.scan_hub_folders.
    """
    from starlette.responses import JSONResponse

    registered = {c.get("source_root") for c in registry_module.all_configs()}

    # P1-2: три обхода каталогов хабов с чтением XML — все в один поток
    def scan_all():
        return {
            "dumps": state.scan_available_dumps(registered),
            "ext": state.scan_hub_folders(state.EXT_HUB),
            "docs": state.scan_hub_folders(state.DOCS_HUB),
        }

    return JSONResponse(await asyncio.to_thread(scan_all))


# Файл переменных для compose (маунт ./.env:/env/.env:rw); COMPASS_ENV_FILE —
# override для хост-запуска/тестов. Файл (ENV_FILE) живёт в main.py: тесты
# патчат src.main.ENV_FILE — сюда он попадает accessor'ом env_file.
_HUB_ENV_KEYS = (("dumps_hub", "DUMPS_HUB"),
                 ("ext_hub", "EXT_HUB"),
                 ("docs_hub", "DOCS_HUB"))


def upsert_env_keys(text: str, updates: dict[str, str]) -> tuple[str, list[str]]:
    """Строчный upsert в тексте .env (compass-85s): незакомментированная
    строка KEY=... заменяется на месте, отсутствующие ключи добавляются
    в конец; остальные строки и комментарии не трогаются.

    Возвращает (новый текст, записанные ключи в порядке updates).
    """
    saved: list[str] = []
    lines = text.splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{key}={remaining.pop(key)}"
            saved.append(key)
    for key, val in remaining.items():
        lines.append(f"{key}={val}")
        saved.append(key)
    new_text = "\n".join(lines)
    if text.endswith("\n") or not text:
        new_text += "\n"
    return new_text, saved


async def api_settings_hubs(request):
    """Сохранить каталоги хабов из панели (compass-85s): перезаписать
    ТОЛЬКО DUMPS_HUB/EXT_HUB/DOCS_HUB в /env/.env (маунт rw), остальные
    строки и комментарии — как были; перед записью копия
    .env.bak-<unix-ts> рядом. Применение — после рестарта контейнера
    (compose перечитывает .env при up): docker compose up -d
    --force-recreate. Валидация: непустые значения без переносов строк.
    Body: {"dumps_hub"?, "ext_hub"?, "docs_hub"?}.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)

    updates: dict[str, str] = {}
    for field, key in _HUB_ENV_KEYS:
        raw = body.get(field)
        if raw is None:
            continue
        val = str(raw).strip()
        if not val:
            return JSONResponse(
                {"error": f"{field}: значение не может быть пустым"},
                status_code=400)
        if "\n" in val or "\r" in val:
            return JSONResponse(
                {"error": f"{field}: переносы строк недопустимы"},
                status_code=400)
        updates[key] = val
    if not updates:
        return JSONResponse(
            {"error": "нет полей для сохранения (dumps_hub/ext_hub/docs_hub)"},
            status_code=400)

    env_file = _deps["env_file"]()
    if env_file.is_dir():
        # маунт несуществующего файла Docker создаёт каталогом
        return JSONResponse(
            {"error": f"{env_file} — каталог: файла .env нет на хосте "
                      "(выполните cp .env.example .env и пересоздайте "
                      "контейнер)"},
            status_code=409)
    if env_file.exists():
        backup = env_file.with_name(f".env.bak-{int(time.time())}")
        shutil.copy2(env_file, backup)
        text = env_file.read_text(encoding="utf-8")
    else:
        text = ""  # файла нет — создадим с этими ключами (бэкап не нужен)
    new_text, saved = upsert_env_keys(text, updates)
    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return JSONResponse(
            {"error": f"запись {env_file} не удалась: {exc}"},
            status_code=500)
    return JSONResponse({
        "saved": saved,
        "applied": "после рестарта контейнера "
                   "(docker compose up -d --force-recreate)",
    })


async def api_config_add(request):
    """Добавить конфигурацию: short_name + выгрузка → новый ID.

    Body: {"short_name": "UT", "source_root": "/workspace/Configurations/X",
           "display_name": "УТ 11.5", "mode": "fast"|"full"}
    Валидация: short_name по правилам, выгрузка существует и содержит
    Configuration.xml, дубликаты short_name/source_root отклоняются.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    short_name = (body.get("short_name") or "").strip()
    source_root = (body.get("source_root") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    mode = body.get("mode", "fast")
    if mode not in ("fast", "full"):
        return JSONResponse({"error": "mode: fast|full"}, status_code=400)
    if not short_name or not source_root:
        return JSONResponse(
            {"error": "short_name и source_root обязательны"}, status_code=400)

    dump_root = Path(source_root)
    if not dump_root.is_dir():
        return JSONResponse({"error": f"выгрузка не найдена: {source_root}"},
                            status_code=400)
    if not (dump_root / "Configuration.xml").exists():
        return JSONResponse({"error": "в папке нет Configuration.xml "
                                      "(не выгрузка конфигурации)"},
                            status_code=400)

    # дубликаты: source_root и short_name
    for c in registry_module.all_configs():
        if c.get("source_root") == source_root:
            return JSONResponse(
                {"error": f"эта выгрузка уже зарегистрирована ({c['id']})"},
                status_code=400)

    result = await asyncio.to_thread(
        registry_module.add_config, short_name, source_root,
        display_name=display_name, mode=mode)  # P1-2: обход дерева — в поток
    if "error" in result:
        return JSONResponse(result, status_code=400)
    cid = result["id"]
    # привязать к контексту панели: сразу видно в таблице как активную
    from .context import bind_default, bind_context, PANEL_CONTEXT
    bind_default(cid)
    bind_context(cid, PANEL_CONTEXT)
    registry_tools.reset_runtime_caches()
    return JSONResponse({"added": cid, "entry": result["entry"]})


async def api_config_move_index(request):
    """Перенести индекс конфигурации в volume /data/index/<cid> (F9/06k):
    файл + правка реестра; reader-кэш сбрасывается — панель видит сразу.
    Body: {id}.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    # P1-2: копирование index.db в volume — гигабайты диска, в поток
    report = await asyncio.to_thread(registry_module.move_index_to_volume, cid)
    if "moved" in report:
        registry_tools.reset_runtime_caches()
    code = 409 if "error" in report else 200
    return JSONResponse(report, status_code=code)


async def api_config_update(request):
    """Обновить конфигурацию: display_name и/или режим поиска.

    Body: {"id": "Compass_UT_a3f9c2d8", "display_name": "УТ 11.5",
           "mode": "fast"|"full"}
    ID неизменяем — на нём завязаны индекс и коллекции Qdrant.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = body.get("id", "")
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.update_config(cid,
                                    display_name=body.get("display_name"),
                                    mode=body.get("mode"))
    if "error" in result:
        return JSONResponse(result, status_code=400)
    registry_tools.reset_runtime_caches()
    return JSONResponse({"updated": cid, "mode": body.get("mode")})


async def api_config_use(request):
    """Переключить активную конфигурацию реестра."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if state.BUILD_STATE["building"]:
        return JSONResponse(
            {"error": "идёт сборка индекса; переключение заблокировано"},
            status_code=409)
    cid = request.query_params.get("id", "")
    result = registry_tools.config_use(cid)
    code = 200 if "active" in result else 404
    return JSONResponse(result, status_code=code)


async def api_config_delete(request):
    """Удалить конфигурацию из реестра. Body: {"id": ...}.

    Индексы и коллекции Qdrant на диске НЕ трогаются (только запись
    реестра + отвязка чатов); физическую очистку — отдельная операция.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.delete_config(cid)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    registry_tools.reset_runtime_caches()
    return JSONResponse(result)


# ------------------------------------------- стек расширений (Т3, панель)

async def api_config_extensions(request):
    """Стек расширений конфигурации: эффективный порядок (v0.5) + счётчики
    из её индекса + расхождения имён стек/индекс (панель «Расширения»)."""
    import sqlite3 as _sqlite3
    from starlette.responses import JSONResponse

    from .recon.gyr_reader import GyrReader
    cid = request.query_params.get("id", "")
    entry = registry_module.get_config(cid)
    if entry is None:
        return JSONResponse(
            {"error": f"конфигурация '{cid}' не найдена"}, status_code=404)
    stack = registry_module.stack_of(cid)
    counts: list = []
    warnings: list = []
    db = Path(entry.get("index_db", ""))
    if db.exists():
        try:
            rd = GyrReader(db)
            counts = rd.extension_counts()
            rd.close()
        except _sqlite3.Error as exc:
            warnings.append({"kind": "index_error",
                             "text": f"индекс не читается: {exc}"})
    index_names = {e["name"] for e in counts}
    stack_names = {e["name"] for e in stack}
    for n in sorted(index_names - stack_names):
        warnings.append({"extension": n, "kind": "index_only",
                         "text": f"«{n}» найдено в индексе, но нет в стеке — "
                                 "добавьте (нужны позиция и назначение)"})
    for n in sorted(stack_names - index_names):
        warnings.append({"extension": n, "kind": "stack_only",
                         "text": f"«{n}» есть в стеке, но не найден в "
                                 "индексе — пересоберите индекс с "
                                 "ext/-соседом выгрузки"})
    confirmed = bool(stack) and all(e.get("order_confirmed") for e in stack)
    return JSONResponse({"id": cid, "stack": stack, "extensions": counts,
                         "warnings": warnings,
                         "order_confirmed": confirmed})


async def api_config_extension_add(request):
    """Добавить расширение в стек: {id, name, position, purpose, status}.

    Директива владельца: позиция и назначение обязательны — при неполных
    полях 400 с текстом директивы (спросить разработчика, не угадывать)."""
    from starlette.responses import JSONResponse

    from .facade.extension_tools import ADD_DIRECTIVE_HINT
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.stack_add(
        cid, body.get("name") or "", body.get("position") or 0,
        body.get("purpose") or "", body.get("status") or "stable")
    if "error" in result:
        return JSONResponse({"error": result["error"],
                             "hint": ADD_DIRECTIVE_HINT}, status_code=400)
    return JSONResponse({"added": result["added"],
                         "stack": registry_module.stack_of(cid)})


async def api_config_extension_remove(request):
    """Убрать расширение из стека конфигурации: {id, name}."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.stack_remove(cid, body.get("name") or "")
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return JSONResponse({"removed": result["removed"],
                         "stack": registry_module.stack_of(cid)})


async def api_config_extension_move(request):
    """Сменить позицию расширения: {id, name, position} (1 = верх списка
    конфигуратора; эффективный порядок пересчитывается внутри назначения)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.stack_move(cid, body.get("name") or "",
                                        body.get("position"))
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return JSONResponse({"moved": result["moved"],
                         "stack": registry_module.stack_of(cid)})


async def api_config_extension_status(request):
    """Переключить статус расширения stable|dev: {id, name, status}."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)
    cid = str(body.get("id") or "").strip()
    if not cid:
        return JSONResponse({"error": "id обязателен"}, status_code=400)
    result = registry_module.stack_set_status(cid, body.get("name") or "",
                                              body.get("status") or "")
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return JSONResponse({"updated": result["updated"],
                         "stack": registry_module.stack_of(cid)})


async def api_config_extension_sync(request):
    """Пинг-синк стека расширений с живой базой (Ф4): Пинг 0.2.0 →
    сверка состава и версий; честная пометка недоступности порядка
    (ПорядокДоступен=Ложь). Body: {id} (по умолчанию — активная
    контекста). Кнопка «Синхронизировать из базы» панели.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # P1-2: Пинг ходит в живую базу 1С (сеть, до 120 с) — в поток
    report = await asyncio.to_thread(extension_tools.extension_sync_from_ping,
                                     cid=str(body.get("id") or "").strip())
    code = 409 if "error" in report or "hint" in report else 200
    return JSONResponse(report, status_code=code)


async def api_data_register(request):
    """Зарегистрировать готовый структурный индекс (index.db от другого
    компаса или из бэкапа) — без пересборки.

    Единый реестровый путь добавления (A2/P10): alias = short_name реестра;
    путь index_db сохраняется как есть (registry.add_config(index_db=...));
    если выгрузка не указана, source_root остаётся пустым — пересборка для
    такой конфигурации будет честно отказывать.

    Body: {"alias": "ut115", "index_db": "/путь/к/index.db",
           "source_root": "/dumps/Atlas_UT_11_5" (необязателен)}
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)

    alias = body.get("alias", "")
    index_db = body.get("index_db", "")
    source_root = body.get("source_root", "")
    if not alias or not index_db:
        return JSONResponse({"error": "alias и index_db обязательны"},
                            status_code=400)

    import sqlite3
    path = Path(index_db)
    if not path.exists():
        return JSONResponse({"error": f"файл не найден: {index_db}"},
                            status_code=404)
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        methods = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        return JSONResponse({"error": f"не индекс gyrfalcon: {exc}"},
                            status_code=400)

    result = registry_module.add_config(alias, source_root,
                                        index_db=index_db)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    cid = result["id"]
    # привязать к контексту панели, как в /api/config/add (R3/A20)
    from .context import bind_default, bind_context, PANEL_CONTEXT
    bind_default(cid)
    bind_context(cid, PANEL_CONTEXT)
    registry_tools.reset_runtime_caches()
    return JSONResponse({"registered": cid, "alias": alias,
                         "methods": methods})


async def api_migrate_vectors(request):
    """Миграция векторов из Community Qdrant (URL) в Pro.

    Body: {"source_url": "http://host:8120", "collection": "code",
           "target_alias": "Atlas_UT_11_5"}
    Читает точки, пересчитывает ID по формуле Pro, пишет в cfg_<алиас>_code.
    Векторы переносятся как есть — ни копейки API эмбеддингов.
    """
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)

    source_url = body.get("source_url", "")
    collection = body.get("collection", "code")
    target_alias = body.get("target_alias") or config.alias
    if not source_url:
        return JSONResponse({"error": "source_url обязателен"},
                            status_code=400)

    # фоновый поток миграции (может занять минуты на 200k+ точек)
    import threading

    def run_migration():
        try:
            migrated = _migrate_vectors(source_url, collection, target_alias)
            print(f"[migrate] готово: {migrated}")
        except Exception as exc:
            print(f"[migrate] ошибка: {exc}")

    threading.Thread(target=run_migration, daemon=True,
                     name="vector-migration").start()
    return JSONResponse({"status": "started",
                         "source": f"{source_url}/{collection}",
                         "target": f"cfg_{target_alias}_{collection}"})


def _migrate_vectors(source_url: str, collection: str, target_alias: str) -> dict:
    """Перенос точек из внешнего Qdrant в Pro с пересчётом ID.

    Читает точки scroll'ом, находит методы в gyrfalcon-индексе по
    метаданным (name + full_path), считает chunk_id по формуле Pro.
    """
    from qdrant_client import QdrantClient

    from .semantic.indexer import chunk_id
    from .semantic.vector_store import VectorStore

    src = QdrantClient(url=source_url, timeout=120)
    store = VectorStore(url=config.qdrant_url or "http://qdrant:6333")
    target = store.collection(f"cfg_{target_alias}_{collection}")

    reader = tools.reader()
    if reader is None:
        raise RuntimeError("структурный индекс не настроен")

    # карта: (name, rel_path) → id метода для gyrfalcon
    methods = {}
    for row in reader.methods_page(limit=500000):
        methods[(row["name"], row["rel_path"])] = row

    migrated = skipped = 0
    cursor = None
    batch_ids, batch_docs, batch_meta, batch_vecs = [], [], [], []

    while True:
        points, cursor = src.scroll(collection, limit=256, offset=cursor,
                                    with_payload=True, with_vector=True)
        if not points:
            break
        for p in points:
            payload = p.payload or {}
            name = payload.get("name", "")
            full_path = payload.get("full_path", "")
            # gyrfalcon хранит rel_path; Community — полный путь от дампа
            rel = full_path.split("/data/source/")[-1] if "/data/source/" in full_path else full_path
            method = methods.get((name, rel))
            if method is None:
                skipped += 1
                continue
            new_id = chunk_id(method["rel_path"], method["name"])
            batch_ids.append(new_id)
            batch_docs.append(payload.get("document", ""))
            batch_meta.append({
                "name": method["name"],
                "full_path": method["rel_path"],
                "module_type": method["module_type"],
                "object_name": method["object_name"],
                "is_export": bool(method["is_export"]),
            })
            batch_vecs.append(p.vector)
            migrated += 1

        if batch_ids:
            target.upsert(ids=batch_ids, documents=batch_docs,
                          metadatas=batch_meta, embeddings=batch_vecs)
            batch_ids, batch_docs, batch_meta, batch_vecs = [], [], [], []

        if cursor is None:
            break

    return {"migrated": migrated, "skipped": skipped}


# ---------------------------------------------------- базовые настройки


async def api_settings_embedding(request):
    """Эмбеддинг-модель: одна на все конфигурации (settings.json["embedding"]).

    Body: {"base_url"?, "model", "api_key"?, "recreate"?}. Смена модели/
    base_url при существующих векторах → 409 needs_confirmation: старые
    векторы несовместимы (и chunk_id не содержит модель — resume их
    пропустит), требуется пересоздание коллекций. recreate: true — удалить
    коллекции Qdrant best-effort и обнулить счётчики векторов в реестре.
    api_key сохраняется в settings.json и наружу не возвращается.
    """
    from starlette.responses import JSONResponse

    from .semantic.vector_store import VectorStore

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body JSON обязателен"}, status_code=400)

    model = str(body.get("model") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "")
    if not model:
        return JSONResponse({"error": "model обязателен"}, status_code=400)

    current = config.embedding_settings()
    changed = bool(
        (current["model"] and current["model"] != model)
        or (current["base_url"] and current["base_url"] != base_url))
    has_vectors = any((c.get("vectors") or {}).get("points")
                      for c in registry_module.all_configs())
    if changed and has_vectors and not body.get("recreate"):
        return JSONResponse({
            "needs_confirmation": True,
            "message": ("Смена эмбеддинг-модели делает существующие векторы "
                        "несовместимыми. Коллекции Qdrant будут удалены, "
                        "векторизацию придётся запустить заново для всех "
                        "конфигураций. Продолжить?"),
        }, status_code=409)

    settings = workspace.load_settings()
    settings["embedding"] = {"base_url": base_url, "model": model,
                             "api_key": api_key}
    workspace.save_settings(settings)

    deleted: list[str] = []
    recreate_error = ""
    if body.get("recreate"):
        try:
            store = VectorStore(url=config.qdrant_url or None,
                                path="/data/qdrant")
            deleted = store.delete_prefixed_code()
        except Exception as exc:
            # Qdrant мог быть недоступен — сохранение настроек не отменяем
            recreate_error = f"{type(exc).__name__}: {exc}"
        reg = workspace.load_registry()
        for entry in (reg.get("configs") or {}).values():
            vectors = entry.get("vectors") or {}
            if vectors.get("points"):
                vectors["points"] = {}
        workspace.save_registry(reg)

    semantic_tools.reset_semantic_stack()
    return JSONResponse({
        "saved": True,
        "source": "settings",
        "deleted_collections": deleted,
        "recreate_error": recreate_error,
        "message": ("Модель сохранена. Для применения переиндексируйте "
                    "векторы каждой конфигурации (⚡ Векторизовать)."),
    })


async def api_links(request):
    """Ссылки проекта для сайдбара: GitHub (+счётчик звёзд) и Telegram."""
    from starlette.responses import JSONResponse

    from . import links
    # github_info ходит в сеть — в поток (R6/A7), не блокируя event loop
    github = await asyncio.to_thread(links.github_info)
    return JSONResponse({
        "github": github,
        "telegram": links.telegram_url(),
    })


async def api_version_check(request):
    """Бейдж новой версии (Волна 1 п.8): /releases/latest GitHub с кэшем;
    недоступность — молчаливый fallback (бейдж не показывается)."""
    from starlette.responses import JSONResponse

    from . import links
    # P1-2: /releases/latest GitHub — сеть в event loop недопустима
    latest = await asyncio.to_thread(links.latest_release) or {}
    tag = latest.get("tag") or ""
    current = APP_VERSION
    out = {"current": current, "latest": tag,
           "url": latest.get("url") or "",
           "update_available": bool(tag) and
           tag.lstrip("vV") != current.lstrip("vV")}
    return JSONResponse(out)


async def api_docs_sync(request):
    """Запустить выгрузку/обновление корпуса документации ИТС (фон)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(help_tools.help_sync())


async def api_docs_pause(request):
    """Пауза выгрузки документации: клиент замирает перед следующим запросом."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(help_tools.help_sync_pause())


async def api_docs_resume(request):
    """Продолжить выгрузку документации после паузы."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(help_tools.help_sync_resume())


async def api_docs_remove(request):
    """Отвязать help-источник от индекса (remedy «принадлежит другому проекту»)."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    source_id = str(body.get("source_id") or "ka25doc")
    return JSONResponse(help_tools.help_remove(source_id))


async def api_semantic_index(request):
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(semantic_tools.start_semantic_index())


async def api_semantic_pause(request):
    """Пауза векторизации: поток замирает перед следующим батчем."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(semantic_tools.pause_semantic_index())


async def api_semantic_resume(request):
    """Продолжить векторизацию после паузы."""
    from starlette.responses import JSONResponse

    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(semantic_tools.resume_semantic_index())


# Таблица панельных роутов: порядок регистрации = прежний порядок
# декораторов в main.py (Starlette матчит роуты в порядке добавления).
_PANEL_ROUTES: tuple = (
    ("/ui", ["GET"], panel_ui),
    ("/assets/{path:path}", ["GET"], panel_assets),
    ("/logo.jpg", ["GET"], panel_logo),
    ("/api/status", ["GET"], api_status),
    ("/api/journal", ["GET"], api_journal),
    ("/api/offline/tasks", ["GET"], api_offline_tasks),
    ("/api/offline/tasks/{task_id}", ["GET"], api_offline_task),
    ("/api/offline/tasks/{task_id}/cancel", ["POST"], api_offline_task_cancel),
    ("/api/offline/tasks/{task_id}/resubmit", ["POST"], api_offline_task_resubmit),
    ("/api/offline/requests/{name}", ["GET"], api_offline_request_download),
    ("/api/offline/responses", ["POST"], api_offline_response_upload),
    # аудит 4l5 P3-1: HEAD — health-check без записи и без токена
    ("/api/offline/health", ["GET", "HEAD"], api_offline_health),
    ("/api/bootstrap", ["POST"], api_bootstrap),
    ("/api/backups", ["GET"], api_backups),
    ("/api/backup", ["POST"], api_backup_create),
    ("/api/backups/restore-semantic", ["POST"], api_backup_restore_semantic),
    ("/api/inbox", ["GET"], api_inbox),
    ("/api/inbox/apply", ["POST"], api_inbox_apply),
    ("/api/backup/restore", ["POST"], api_backup_restore),
    ("/api/dumps/available", ["GET"], api_dumps_available),
    ("/api/settings/hubs", ["POST"], api_settings_hubs),
    ("/api/config/add", ["POST"], api_config_add),
    ("/api/config/move-index", ["POST"], api_config_move_index),
    ("/api/config/update", ["POST"], api_config_update),
    ("/api/config/use", ["POST"], api_config_use),
    ("/api/config/delete", ["POST"], api_config_delete),
    ("/api/config/extensions", ["GET"], api_config_extensions),
    ("/api/config/extension/add", ["POST"], api_config_extension_add),
    ("/api/config/extension/remove", ["POST"], api_config_extension_remove),
    ("/api/config/extension/move", ["POST"], api_config_extension_move),
    ("/api/config/extension/status", ["POST"], api_config_extension_status),
    ("/api/config/extension/sync", ["POST"], api_config_extension_sync),
    ("/api/data/register", ["POST"], api_data_register),
    ("/api/data/migrate-vectors", ["POST"], api_migrate_vectors),
    ("/api/settings/embedding", ["POST"], api_settings_embedding),
    ("/api/links", ["GET"], api_links),
    ("/api/version/check", ["GET"], api_version_check),
    ("/api/docs/sync", ["POST"], api_docs_sync),
    ("/api/docs/pause", ["POST"], api_docs_pause),
    ("/api/docs/resume", ["POST"], api_docs_resume),
    ("/api/docs/remove", ["POST"], api_docs_remove),
    ("/api/semantic/index", ["POST"], api_semantic_index),
    ("/api/semantic/pause", ["POST"], api_semantic_pause),
    ("/api/semantic/resume", ["POST"], api_semantic_resume),
)


def register_panel_routes(mcp_app, *, start_build, build_queue, env_file) -> None:
    """Зарегистрировать панельные роуты на FastMCP-приложении (вызов из main).

    Параметры:
        mcp_app — FastMCP-инстанс (main.mcp);
        start_build — запуск сборки индекса (main._start_build_locked):
            единый вход с очередью и память-гардой;
        build_queue — список main._build_queue (ссылка: /api/status читает
            актуальное состояние, main мутирует на месте);
        env_file — callable → Path .env-файла compose (main.ENV_FILE;
            accessor ради патчей src.main.ENV_FILE в тестах).

    Побочные эффекты: заполняет модульный _deps (обработчики берут
    зависимости оттуда), добавляет HTTP-роуты в mcp_app.
    """
    _deps.update(start_build=start_build, build_queue=build_queue,
                 env_file=env_file)
    for path, methods, handler in _PANEL_ROUTES:
        # аудит 4l5 P1-1: мутирующие роуты — за Origin/Host/Content-Type
        # гейтом (обёртка _guarded); GET-статика гейта не касается
        target = _guarded(handler) if "POST" in methods else handler
        mcp_app.custom_route(path, methods=methods)(target)
