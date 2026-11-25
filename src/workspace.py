# -*- coding: utf-8 -*-
"""Системное состояние компаса: реестр/настройки/сессии в /data/system.

S3 (arch-storage v0.2 §2): концепция «рабочего каталога» WORKSPACE_HUB
выпилена. Три файла состояния живут в фиксированном
/data/system/settings/ (volume, переживает пересоздание контейнера);
env-override — только для тестов и хост-запуска:
  COMPASS_SYSTEM_ROOT    корень системной зоны (дефолт /data/system)
  COMPASS_SETTINGS_PATH  точечный override settings-каталога
Пути резолвятся НА КАЖДЫЙ вызов (env читается в момент обращения —
герметичные тесты ставят override в фикстуре, а не до импорта).
Разовый перенос: при init_files() файлы из legacy /workspace/Settings
(или COMPASS_WORKSPACE_ROOT) переезжают в новое место, если там пусто.
Модуль — read/state-обёртка; досье-оверлеи живут в settings-каталоге
на уровень выше, точки входа дампов — root()/Configurations.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def system_root() -> Path:
    """Корень системной зоны: /data/system (env COMPASS_SYSTEM_ROOT)."""
    return Path(os.getenv("COMPASS_SYSTEM_ROOT") or "/data/system")


def settings_dir() -> Path:
    """Каталог файлов состояния: system/settings (env COMPASS_SETTINGS_PATH)."""
    env = os.getenv("COMPASS_SETTINGS_PATH")
    if env:
        return Path(env)
    return system_root() / "settings"


def registry_path() -> Path:
    return settings_dir() / "registry.json"


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def sessions_path() -> Path:
    return settings_dir() / "sessions.json"


def root() -> Path:
    """Точка входа дампов на контейнере: /workspace (только скан при
    добавлении; рабочим каталогом больше не является, R11/A14 legacy)."""
    return Path(os.getenv("COMPASS_WORKSPACE_ROOT", "/workspace"))


def init_files() -> dict:
    """Создать system/settings/ и три файла состояния, если их нет.

    Идемпотентно: существующие файлы не трогаются. Заодно разовый
    перенос из legacy рабочего каталога (migrate_legacy_settings).
    Вызывается при старте ядра.
    """
    migrated = migrate_legacy_settings()
    target = settings_dir()
    target.mkdir(parents=True, exist_ok=True)
    created = []
    defaults: dict[Path, dict] = {
        registry_path(): {"version": 1, "configs": {}, "active_default": None},
        settings_path(): {"version": 1, "default_config_id": None},
        sessions_path(): {"version": 1, "sessions": {}},
    }
    for path, default in defaults.items():
        if not path.exists():
            path.write_text(json.dumps(default, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            created.append(path.name)
    if migrated:
        created = []  # файлы приехали переносом — «создано» пусто
    return {"created": created, "migrated": migrated}


def migrate_legacy_settings() -> list[str]:
    """Разовый перенос Settings-файлов из /workspace/Settings → новый путь.

    Условия: целевой registry.json не существует, legacy-каталог
    существует. Файлы перемещаются (shutil.move — и кросс-диск ок).
    Обратной миграции нет; перенос идемпотентен (второй вызов — пустой
    список).
    """
    import shutil

    legacy_dir = root() / "Settings"
    target = settings_dir()
    if registry_path().exists() or not legacy_dir.is_dir():
        return []
    target.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in ("registry.json", "settings.json", "sessions.json"):
        src = legacy_dir / name
        dst = target / name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            moved.append(name)
    if moved:
        import logging
        logging.getLogger(__name__).info(
            "Settings перенесены из legacy (%s → %s): %s",
            legacy_dir, target, ", ".join(moved))
    return moved


# Кэш ТЕКСТА файлов состояния: панель опрашивает /api/status каждые 5 с,
# и каждый poll читал sessions.json/registry.json/settings.json по 2–3 раза
# через drvfs (медленное 9P-хранилище). Ключ (mtime_ns, size): stat дешевле
# чтения+парса, а _write (tmp+replace) гарантированно меняет mtime —
# записи через все save_* инвалидируют кэш сами. Кэшируется именно текст:
# json.loads при каждом вызове отдаёт СВЕЖИЙ dict — мутирующие читатели
# (bind_context, слияния) не отравляют кэш.
_TEXT_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _read(path: Path) -> dict:
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    cached = _TEXT_CACHE.get(str(path))
    if cached and cached[0] == key:
        text = cached[1]
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        _TEXT_CACHE[str(path)] = (key, text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _write(path: Path, data: dict) -> None:
    settings_dir().mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ реестр

# Аудит PERF-6: registry.json пишут ядро (registry.py) и CLI-миграция
# (manage.py) из РАЗНЫХ процессов — read-modify-write без межпроцессного
# лока терял апдейты. Кроссплатформенный файл-лок <registry>.lock
# (fcntl/msvcrt, без новых зависимостей) + поток-лок (POSIX-fcntl не
# различает потоки одного процесса). Блокирующая модель с таймаутом:
# не взяли за 10 с — честный отказ, вызывающий возвращает {"error": ...}.
_REGISTRY_LOCK_TIMEOUT = 10.0

_REGISTRY_THREAD_LOCK = threading.Lock()


class RegistryBusy(Exception):
    """Файл-лок реестра не взят за таймаут (занят другим процессом)."""


try:
    import fcntl

    def _file_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _file_unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _file_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _file_unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def registry_lock(timeout: float | None = None):
    """Кросспроцессный файл-лок реестра: acquire → read-modify-write → release.

    Потоки одного процесса — _REGISTRY_THREAD_LOCK, процессы — файл
    <registry>.lock рядом с registry.json. Блокирующий захват с общим
    таймаутом (дефолт _REGISTRY_LOCK_TIMEOUT, ~10 с); RegistryBusy —
    не взяли: вызывающий возвращает честный отказ
    {"error": "реестр занят другим процессом"}.
    """
    deadline = time.monotonic() + (timeout if timeout is not None
                                   else _REGISTRY_LOCK_TIMEOUT)
    if not _REGISTRY_THREAD_LOCK.acquire(
            timeout=max(0.0, deadline - time.monotonic())):
        raise RegistryBusy("реестр занят другим процессом")
    try:
        lock_path = registry_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fh:
            while True:
                try:
                    _file_lock(fh)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RegistryBusy(
                            "реестр занят другим процессом") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                _file_unlock(fh)
    finally:
        _REGISTRY_THREAD_LOCK.release()


def load_registry() -> dict:
    return _read(registry_path())


def save_registry(reg: dict) -> None:
    reg["updated_at"] = datetime_utc()
    reg["version"] = 1
    _write(registry_path(), reg)


def datetime_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ настройки

def load_settings() -> dict:
    return _read(settings_path())


def save_settings(data: dict) -> None:
    current = load_settings()
    current.update(data)
    current["version"] = 1
    _write(settings_path(), current)


def save_build_queue(cids: list[str]) -> None:
    """Очередь сборки в settings.json — merge через update, прочие ключи
    не трогаются (R15/A16)."""
    save_settings({"build_queue": list(cids)})


def load_build_queue() -> list[str]:
    """Очередь сборки из settings.json (порядок сохранён; пусто — нет)."""
    return list(load_settings().get("build_queue") or [])


# ------------------------------------------------------------------ сессии

# Писатели sessions.json живут в разных модулях (context.bind_context,
# session_activity) и потоках (middleware event-loop, тулы) — общий лок
# делает их read-modify-write атомарными (урок: параллельные правки
# одного файла затирают друг друга)
_SESSIONS_LOCK = threading.Lock()


def update_sessions(mutate) -> dict:
    """Атомарно изменить sessions.json: load → mutate(data) → save под локом.

    mutate — функция, изменяющая словарь на месте; исключение откатывает
    операцию (запись не выполняется). Возвращает итоговый словарь.
    """
    with _SESSIONS_LOCK:
        data = load_sessions()
        mutate(data)
        save_sessions(data)
        return data


def load_sessions() -> dict:
    return _read(sessions_path())


def save_sessions(data: dict) -> None:
    data["version"] = 1
    _write(sessions_path(), data)
