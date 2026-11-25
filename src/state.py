# -*- coding: utf-8 -*-
"""Состояние ядра: прогресс сборки + env-переопределения и legacy-остатки.

Идея «пустого контейнера»: системный уровень живёт в env (порт, токен,
провайдер эмбеддингов). Конфигурационный уровень (какой дамп, где индекс)
с 1mj.1/A2 живёт ТОЛЬКО в реестре (registry.py); legacy /data/config.json
читается разовой миграцией реестра — здесь остались env-приоритетные
чтения и поля-остатки для совместимости до полной зачистки.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

def _hub_dir(env_key: str, default: str) -> Path:
    """env-путь хаба, если он существует ЗДЕСЬ, иначе контейнерный
    дефолт (маунт-пойнт, compass-bcl).

    compose пробрасывает DUMPS_HUB/EXT_HUB/DOCS_HUB в environment для
    /api/status.hubs (host-значения из .env); внутри контейнера такие
    пути не существуют — тула (scan_dumps, scan_hub_folders, manage add)
    должны работать по маунт-пойнтам."""
    val = os.getenv(env_key)
    if val and Path(val).is_dir():
        return Path(val)
    return Path(default)


DUMPS_HUB = _hub_dir("DUMPS_HUB", "/dumps")
# Входные хабы расширений и готовых док-корпусов (compass-85s): read-only
# скан при выборе; рабочие копии ядро кладёт в volume (/data/extensions,
# /data/configs/<cid>/help) — хаб-папки после этого можно править/чистить
EXT_HUB = _hub_dir("EXT_HUB", "/ext-dumps")
DOCS_HUB = _hub_dir("DOCS_HUB", "/docs-dumps")
STATE_FILE = Path(os.getenv("COMPASS_STATE_FILE", "/data/config.json"))

# Прогресс сборки индекса (живёт в памяти; переживать рестарт не обязан).
# gen — номер сборки (stale-finish: очнувшийся поток старой сборки не
# перезапишет состояние новой); pid — PID gyrfalcon для watchdog.
# phase (compass-2xo) — фаза для наблюдателя и watchdog: "build" — работает
# воркер gyrfalcon; "post" — воркер вышел (worker_exit), поток идёт по
# пост-шагам (fill_bodies/досье); None — сборка не идёт/завершена.
BUILD_STATE: dict[str, Any] = {
    "building": False,
    "started_at": None,
    "started_epoch": None,
    "finished_at": None,
    "exit_code": None,
    "pid": None,
    "gen": 0,
    "tail": [],  # последние строки вывода билдера
    "phase": None,
    "worker_exit": None,
}
_BUILD_LOCK = threading.Lock()
_BUILD_PROC: subprocess.Popen | None = None

# Публичный хук «кто пишет в общий index.db»: слой справки ставит флаг на
# время fetch (аудит модульности §7 №5 — реестр не читает приватное _docs)
DOCS_BUSY = threading.Event()


def docs_busy() -> bool:
    """Идёт синхронизация документации (общий index.db)."""
    return DOCS_BUSY.is_set()


_STATE: dict[str, Any] = {}
_STATE_LOCK = threading.Lock()


def load_state() -> dict[str, Any]:
    """Прочитать сохранённое состояние (толерантно к отсутствию/битому)."""
    global _STATE
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            loaded = {}
    except (OSError, json.JSONDecodeError):
        loaded = {}
    with _STATE_LOCK:
        _STATE = loaded
    return _STATE


def save_state(update: dict[str, Any]) -> dict[str, Any]:
    """Сохранить состояние атомарной записью (tmp + replace).

    Файловый IO (drvfs — минуты при подвисании) держит ТОЛЬКО
    _STATE_LOCK: _BUILD_LOCK общесборочный, build_finish не должен
    ждать чужой IO (инцидент fc9 — building=True на 1.5 ч).
    """
    global _STATE
    with _STATE_LOCK:
        merged = {**_STATE, **update}
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(STATE_FILE)
        _STATE = merged
    return _STATE


def active_source_root() -> Path | None:
    """Активная выгрузка: env → активная конфигурация реестра → legacy-поле."""
    env = os.getenv("COMPASS_SOURCE_ROOT")
    if env:
        return Path(env)
    st = load_state()
    active = st.get("active")
    if active and active in (st.get("configs") or {}):
        return Path(st["configs"][active]["source_root"])
    val = st.get("source_root")
    return Path(val) if val else None


def active_index_db() -> Path | None:
    """Путь активного индекса: env → реестр → legacy-поле."""
    env = os.getenv("COMPASS_INDEX_DB")
    if env:
        return Path(env)
    st = load_state()
    active = st.get("active")
    if active and active in (st.get("configs") or {}):
        return Path(st["configs"][active]["index_db"])
    val = st.get("index_db")
    return Path(val) if val else None


def active_alias() -> str:
    """Алиас активной конфигурации (для коллекций Qdrant и заголовков)."""
    return (os.getenv("COMPASS_ALIAS")
            or (load_state() or {}).get("active")
            or (load_state() or {}).get("alias")
            or "main")



def scan_dumps() -> list[dict[str, str]]:
    """Каталоги хаба /dumps, являющиеся выгрузками (есть Configuration.xml)."""
    out: list[dict[str, str]] = []
    if not DUMPS_HUB.is_dir():
        return out
    for child in sorted(DUMPS_HUB.iterdir()):
        if child.is_dir() and (child / "Configuration.xml").exists():
            out.append({"alias": child.name, "source_root": str(child)})
    return out


def scan_available_dumps(registered_roots: set[str] | None = None) -> list[dict]:
    """Единый источник выгрузок для окна «Добавить» (R7/A9): папка
    workspace/Configurations И хаб DUMPS_HUB.

    Дедуп по source_root (workspace приоритетнее); поле hub — откуда
    пришла папка ("workspace"|"dumps"); validity — наличие
    Configuration.xml; registered — по переданным source_root.
    """
    from .registry import read_config_info
    from .workspace import root as workspace_root

    registered = registered_roots or set()
    sources = [(workspace_root() / "Configurations", "workspace"),
               (DUMPS_HUB, "dumps")]
    out: list[dict] = []
    seen_roots: set[str] = set()
    for base, hub in sources:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            source_root = str(child)
            if source_root in seen_roots:
                continue
            seen_roots.add(source_root)
            has_xml = (child / "Configuration.xml").exists()
            out.append({
                "source_root": source_root,
                "folder": child.name,
                "hub": hub,
                "valid": has_xml,
                "registered": source_root in registered,
                "info": read_config_info(source_root) if has_xml else {},
            })
    return out


def scan_hub_folders(base: Path) -> list[dict]:
    """Папки входного хаба (EXT_HUB/DOCS_HUB, compass-85s) для выбора
    в панели: имя + путь, по алфавиту. Без валидации содержимого — её
    делает consumer при добавлении (extension_add / help kb sync).
    """
    if not base.is_dir():
        return []
    return [{"folder": child.name, "path": str(child)}
            for child in sorted(base.iterdir()) if child.is_dir()]


def build_begin() -> int:
    """Отметить старт сборки (вызывает обработчик /reindex|bootstrap).

    Возвращает номер сборки (gen): передайте его в build_finish —
    stale-финиш просроченного потока (например, после принудительного
    завершения watchdog'ом) состояние новой сборки не перезапишет.
    """
    with _BUILD_LOCK:
        BUILD_STATE["gen"] = int(BUILD_STATE.get("gen") or 0) + 1
        BUILD_STATE.update(building=True, started_at=_now(),
                           started_epoch=time.time(), finished_at=None,
                           exit_code=None, pid=None, tail=[],
                           phase="build", worker_exit=None)
        return BUILD_STATE["gen"]


def build_finish(exit_code: int, tail: list[str],
                 gen: int | None = None) -> bool:
    """Зафиксировать результат сборки для панели/статуса.

    gen — номер сборки от build_begin; просроченный gen (watchdog уже
    закрыл сборку или началась следующая) игнорируется → False.
    """
    with _BUILD_LOCK:
        if gen is not None and gen != BUILD_STATE.get("gen"):
            return False
        BUILD_STATE.update(building=False, finished_at=_now(),
                           exit_code=exit_code, pid=None,
                           started_epoch=None, tail=tail[-8:],
                           phase=None, worker_exit=None)
        _clear_build_proc()
        return True


def set_build_proc(proc: subprocess.Popen) -> None:
    """Запомнить процесс билдера (PID в BUILD_STATE, объект — для watchdog)."""
    global _BUILD_PROC
    with _BUILD_LOCK:
        _BUILD_PROC = proc
        BUILD_STATE["pid"] = getattr(proc, "pid", None)


def _clear_build_proc() -> None:
    global _BUILD_PROC
    _BUILD_PROC = None


def build_proc_alive() -> bool | None:
    """Жив ли процесс билдера: True/False; None — процесс не отслежен."""
    proc = _BUILD_PROC
    if proc is None:
        return None
    try:
        return proc.poll() is None
    except Exception:
        return None


def build_phase_post(worker_exit_code: int | None = None) -> None:
    """Зафиксировать выход воркера: сборка перешла в пост-шаги (compass-2xo).

    Вызывает поток сборки, когда communicate() вернул управление — воркер
    завершился сам (worker_exit_code — его код) либо убит по нашему
    таймауту, дальше идут пост-шаги (кэш счётчиков, fill_bodies, досье).
    Watchdog в фазе "post" не пишет exit=-1 «не жив» (инцидент 96j: ложный
    −1 висел в /api/status ~20 мин поверх успешного прогона): смертью
    считается только мёртвый процесс в фазе "build", когда поток сам ещё
    не видел завершения воркера.
    """
    with _BUILD_LOCK:
        if not BUILD_STATE.get("building"):
            return  # сборка не идёт/уже закрыта — помечать нечего
        BUILD_STATE.update(
            phase="post", worker_exit=worker_exit_code,
            tail=[f"воркер завершён (EXIT={worker_exit_code}), "
                  "идут пост-шаги сборки"])


# ------------------------------------------------------- watchdog сборки

def watchdog_minutes() -> int:
    """Порог зависшей сборки в минутах (env BUILD_WATCHDOG_MIN, дефолт 15)."""
    try:
        val = int(os.getenv("BUILD_WATCHDOG_MIN", "15"))
    except ValueError:
        return 15
    return max(1, val)


def watchdog_needed(building: bool, started_epoch: float | None,
                   proc_alive: bool | None, now_ts: float,
                   min_minutes: int, phase: str | None = "build") -> bool:
    """Нужен ли принудительный finish (чистая функция — тестируется).

    Стреляем только когда точно знаем, что процесс мёртв: живой билдер
    (True) или не отслежен (None) — не трогаем, вслепую не убиваем.
    Фаза "post" (compass-2xo) смертью не считается: воркер завершился
    сам, поток сборки видит это и идёт по пост-шагам — exit=-1 не пишем.
    """
    if not building or started_epoch is None or proc_alive is not False:
        return False
    if phase == "post":
        return False
    return (now_ts - started_epoch) >= max(1, min_minutes) * 60


def build_watchdog_tick(now_ts: float | None = None) -> dict[str, Any]:
    """Один тик watchdog'а: building=True + мёртвый gyrfalcon > X минут
    → принудительный build_finish с пометкой в tail.

    Исключение (compass-2xo): фаза "post" — воркер завершился сам, поток
    сборки дошёл до пост-шагов; даже если процесс не жив и возраст больше
    порога, exit=-1 не пишем (наблюдателю видна фаза в build-блоке).
    Вызывается фоновым потоком раз в минуту (start_build_watchdog).
    Возвращает отчёт для логов/тестов, ошибок не поднимает.
    """
    try:
        now = time.time() if now_ts is None else now_ts
        snap = dict(BUILD_STATE)
        alive = build_proc_alive()
        minutes = watchdog_minutes()
        if not watchdog_needed(snap.get("building"),
                               snap.get("started_epoch"), alive, now,
                               minutes, snap.get("phase")):
            reason = ("воркер завершился сам, идут пост-шаги (phase=post)"
                      if snap.get("phase") == "post"
                      else "не подходит под критерии")
            return {"checked": True, "finished": False, "reason": reason}
        age_min = round((now - snap["started_epoch"]) / 60, 1)
        note = (f"watchdog: building=True {age_min} мин, "
                f"процесс gyrfalcon (pid={snap.get('pid')}) не жив — "
                "принудительное завершение сборки (fc9)")
        print("[watchdog]", note)
        finished = build_finish(-1, [note])
        return {"checked": True, "finished": finished, "note": note,
                "age_min": age_min, "pid": snap.get("pid")}
    except Exception as exc:  # watchdog не должен умирать от разового сбоя
        print("[watchdog] ошибка тика:", type(exc).__name__, exc)
        return {"checked": False, "finished": False,
                "error": f"{type(exc).__name__}: {exc}"}


def start_build_watchdog(tick_seconds: int = 60) -> threading.Thread:
    """Запустить daemon-поток watchdog'а (тик раз в tick_seconds секунд).

    Отдельный поток, а не тик в RebuildScheduler: автосборка может быть
    выключена (COMPASS_REBUILD_SCHEDULE=0), а виснуть сборка может всегда.
    """
    def _loop() -> None:
        while True:
            time.sleep(tick_seconds)
            build_watchdog_tick()

    thread = threading.Thread(target=_loop, daemon=True,
                              name="build-watchdog")
    thread.start()
    return thread


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
