# -*- coding: utf-8 -*-
"""Реестр конфигураций: Compass_<short>_<uid>, описательная часть, векторы.

ID конфигурации неизменяем: генерируется при добавлении и никогда не
меняется — на нём завязаны папка индекса и имена Qdrant-коллекций.
Переименование в панели меняет только display_name.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path

from .workspace import (RegistryBusy, datetime_utc, load_registry,
                        registry_lock, save_registry)

logger = logging.getLogger(__name__)
# Горячие данные (индексы) — в volume /data/index (ext4 против drvfs,
# F9/06k); env COMPASS_INDEX_ROOT — для хост-запуска и тестов
INDEX_ROOT = Path(os.getenv("COMPASS_INDEX_ROOT", "/data/index"))

SHORT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
ID_RE = re.compile(r"^Compass_[A-Za-z0-9_-]{1,16}_[0-9a-f]{8}$")


# ------------------------------------------------------- хабы volume (S1)
#
# Целевая схема arch-storage v0.2 §1: три хаба в volume /data —
#   configs/<cid>/{index.db, source/}   индекс + рабочая копия выгрузки
#   docs/<cid>/<source_id>/             корпуса документации ИТС
#   extensions/<cid>/<Имя>/             исходники расширений
# Корни читаются из env НА КАЖДЫЙ вызов: COMPASS_CONFIGS_ROOT /
# COMPASS_DOCS_ROOT / COMPASS_EXTENSIONS_ROOT — override для тестов и
# хост-запуска (S3 фиксирует тот же стиль для system/*).

def configs_root() -> Path:
    return Path(os.getenv("COMPASS_CONFIGS_ROOT") or "/data/configs")


def docs_root() -> Path:
    return Path(os.getenv("COMPASS_DOCS_ROOT") or "/data/docs")


def extensions_root() -> Path:
    return Path(os.getenv("COMPASS_EXTENSIONS_ROOT") or "/data/extensions")


def config_home(cid: str) -> Path:
    """Дом конфигурации: configs/<cid>/."""
    return configs_root() / cid


def config_index_db(cid: str) -> Path:
    return config_home(cid) / "index.db"


def config_source_root(cid: str) -> Path:
    return config_home(cid) / "source"


def config_docs_root(cid: str, source_id: str = "ka25doc") -> Path:
    """Корпус документации конфигурации: docs/<cid>/<source_id>/."""
    return docs_root() / cid / source_id


def config_extension_root(cid: str, name: str = "") -> Path:
    """Исходники расширения: extensions/<cid>/<Имя>/ (name пуст — хаб cid)."""
    return extensions_root() / cid / name


def source_version(root: str | Path) -> str | None:
    """Дайджест дерева выгрузки («версия исходника», спека §3-2).

    sha256 по отсортированным (relpath, size) всех файлов — содержимое
    не читается (выгрузка — десятки тысяч файлов); замена выгрузки
    меняет размеры/состав → версия меняется. Пишется в запись реестра
    при сборке (set_indexed) и добавлении; панель сравнивает с текущим
    пересчётом — «исходник новее индекса». None — корня нет/не папка.
    """
    import hashlib

    base = Path(root)
    if not base.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue  # симлинк ext/ — не часть тела выгрузки
        try:
            rel = path.relative_to(base).as_posix()
            size = path.stat().st_size
        except OSError:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def ensure_ext_link(cid: str) -> dict:
    """Симлинк source/ext → extensions/<cid>/ (спека §1: gyrfalcon ищет
    ext/ соседом выгрузки — физика сборки не меняется).

    Создаётся при первой индексации расширяемой конфигурации (вызывается
    из потока сборки перед gyrfalcon). Идемпотентен. Условия: дом-схема
    путей (source внутри configs/<cid>) и непустой стек расширений —
    legacy-записи и конфигурации без расширений не трогаются (S4).
    Windows: симлинк требует привилегию/developer mode — ошибка
    возвращается честно, сборка продолжается без ext (задача явно
    сообщает «без симлинка расширения в сборку не попадут»).
    """
    entry = get_config(cid)
    if entry is None:
        return {"error": f"конфигурация '{cid}' не найдена"}
    source = Path(entry.get("source_root") or "")
    if source.resolve() != config_source_root(cid).resolve():
        return {"skipped": "пути не дом-схемы — симлинк ставит миграция (S4)"}
    if not (entry.get("stack") or []):
        return {"skipped": "стек расширений пуст"}
    ext_hub = extensions_root() / cid
    link = source / "ext"
    if link.is_symlink() or link.exists():
        return {"ok": "симлинк уже на месте", "link": str(link)}
    try:
        ext_hub.mkdir(parents=True, exist_ok=True)
        os.symlink(ext_hub, link, target_is_directory=True)
    except OSError as exc:
        return {"error": (f"симлинк {link} → {ext_hub} не создан: "
                          f"{type(exc).__name__}: {exc} — расширения в "
                          "сборку не попадут")}
    return {"ok": "симлинк создан", "link": str(link),
            "target": str(ext_hub)}


def gen_config_id(short_name: str) -> str:
    """Compass_<short>_<8 hex>: читаемо в путях, уникально случайно."""
    return f"Compass_{short_name}_{secrets.token_hex(4)}"


def read_config_info(source_root: str) -> dict:
    """Name/Version/Platform из Configuration.xml выгрузки."""
    import xml.etree.ElementTree as Et

    path = Path(source_root) / "Configuration.xml"
    if not path.exists():
        return {}
    try:
        root = Et.parse(path).getroot()
    except Et.ParseError:
        return {}
    def local(tag: str) -> str:
        return tag.split("}")[-1]

    info: dict[str, str] = {}
    for el in root.iter():
        if local(el.tag) == "Properties":
            for child in el:
                ct = local(child.tag)
                if ct in ("Name", "Version",
                          "ConfigurationExtensionCompatibilityMode"):
                    info[ct] = (child.text or "").strip()
    mode = info.get("ConfigurationExtensionCompatibilityMode", "")
    info["Platform"] = (mode.replace("Version", "").replace("_", ".")
                        if mode.startswith("Version") else mode)
    return info


def add_config(short_name: str, source_root: str | None = None,
               display_name: str = "", mode: str = "fast",
               index_db: str | None = None) -> dict:
    """Добавить конфигурацию: сгенерировать ID, прочитать Configuration.xml.

    Пути по умолчанию — дом-схема (arch-storage v0.2): index в
    configs/<cid>/index.db, рабочая копия выгрузки configs/<cid>/source/
    (копирование дамп-хаба → source/ делает вызывающий — CLI add (S5)
    или панель; хост-дамп после копирования можно удалить). Явный
    source_root/index_db сохраняется как есть — legacy-записи (их
    переносит migrate-to-homes, S4), доменные пути для них не подставляются.
    Возвращает {"id": ...} или {"error": ...}.
    """
    if not SHORT_NAME_RE.fullmatch(short_name):
        return {"error": (f"short_name '{short_name}' не соответствует "
                          "правилам: латиница/цифры/дефис, 1–16 символов")}
    try:
        with registry_lock():
            reg = load_registry()
            taken = {c.get("short_name")
                     for c in (reg.get("configs") or {}).values()}
            if short_name in taken:
                return {"error": f"short_name '{short_name}' уже используется"}

            uid = secrets.token_hex(4)
            cid = f"Compass_{short_name}_{uid}"
            info_src = source_root if source_root else config_source_root(cid)
            info = read_config_info(info_src)
            display = display_name or info.get("Name", short_name)
            src = source_root or str(config_source_root(cid))
            idx = index_db or str(config_index_db(cid))
            entry = {
                "short_name": short_name,
                "display_name": display,
                "source_root": src,
                "index_db": idx,
                "mode": mode,
                "config_info": info,
                "vectors": {"collection_prefix": cid, "model": None,
                            "dimension": None, "points": {}},
                "created_at": datetime_utc(),
                "indexed_at": None,
                "source_version": source_version(src),
            }
            reg.setdefault("configs", {})[cid] = entry
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"id": cid, "entry": entry}


def update_config(cid: str, display_name: str | None = None,
                  mode: str | None = None) -> dict:
    """Обновить редактируемые поля (display_name, mode). ID не меняется."""
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            if mode is not None:
                if mode not in ("fast", "full"):
                    return {"error": "mode: fast|full"}
                entry["mode"] = mode
            if display_name is not None:
                if not display_name.strip():
                    return {"error": "display_name не может быть пустым"}
                entry["display_name"] = display_name.strip()
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"updated": cid}


def delete_config(cid: str) -> dict:
    """Удалить конфигурацию из реестра (индексы на диске не трогаются).

    Сопутствующая чистка: если удалённая была дефолтом — дефолт сбрасывается;
    привязки чатов (sessions.json) на эту конфигурацию отвязываются —
    иначе тулы этих чатов молча упадут на несуществующий ID.
    """
    try:
        with registry_lock():
            reg = load_registry()
            if cid not in (reg.get("configs") or {}):
                return {"error": f"конфигурация '{cid}' не найдена"}
            del reg["configs"][cid]

            # дефолт для контекста живёт в settings.json (registry.active_default —
            # legacy от миграции, чистим тоже)
            from . import workspace
            settings = workspace.load_settings()
            was_default = settings.get("default_config_id") == cid
            if was_default:
                settings["default_config_id"] = None
                workspace.save_settings(settings)
            if reg.get("active_default") == cid:
                reg["active_default"] = None
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}

    unbound: list[str] = []

    def _unbind(data: dict) -> None:
        sessions = data.setdefault("sessions", {})
        for chat, rec in list(sessions.items()):
            if (rec or {}).get("config_id") == cid:
                sessions.pop(chat, None)
                unbound.append(chat)

    workspace.update_sessions(_unbind)
    return {"deleted": cid, "was_default": was_default,
            "unbound_sessions": sorted(unbound)}


def get_config(cid: str) -> dict | None:
    return (load_registry().get("configs") or {}).get(cid)


def all_configs() -> list[dict]:
    reg = load_registry()
    out = []
    for cid, entry in sorted((reg.get("configs") or {}).items()):
        out.append({"id": cid, **entry})
    return out


def set_indexed(cid: str, counts: dict | None = None) -> None:
    """Отметить успешную сборку: время + кэш счётчиков + версия исходника.

    counts (methods/modules/calls/...) пишется в запись — статусные
    эндпоинты читают кэш, не выполняя COUNT'ы по медленному хранилищу.
    source_version — дайджест дерева source/ НА МОМЕНТ сборки: панель
    сравнивает с живым пересчётом и честно показывает «исходник новее
    индекса» (спека §3-2).
    """
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is not None:
                entry["indexed_at"] = datetime_utc()
                entry["source_version"] = source_version(
                    entry.get("source_root"))
                if counts:
                    entry["index_counts"] = counts
                save_registry(reg)
    except RegistryBusy:
        logger.warning("set_indexed: реестр занят другим процессом (%s)", cid)


def set_collection_name(cid: str, name: str) -> None:
    """Зафиксировать фактическое имя Qdrant-коллекции (R2/A1).

    Имя пишется в entry["vectors"]["collection_name"] при первом обращении
    и больше не меняется: переименование конфигурации осиротило бы точки.
    Полная миграция точек не делается — см. план R2 (дефект дублирования
    при переименовании описан в плане, опция владельца).
    """
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is not None:
                entry.setdefault("vectors", {})["collection_name"] = name
                save_registry(reg)
    except RegistryBusy:
        logger.warning(
            "set_collection_name: реестр занят другим процессом (%s)", cid)


def collection_name(cid: str | None = None) -> str:
    """Имя векторной коллекции конфигурации: зафиксированное в реестре
    (R2/A1, переживает переименования), иначе cfg_<cid>_code; без cid —
    активная конфигурация; без активной — легаси cfg_<alias>_code.
    Публичная точка для слоёв (бэкапы) — аудит модульности §7 №6.

    Семантика зеркалит прежнюю facade.semantic_tools._collection_name:
    cid задан, но записи в реестре нет → тоже легаси-ветка через alias.
    config импортируется лениво — без цикла на уровне модулей."""
    if not cid:
        from .config import config
        cid = config.active_config_id
    entry = get_config(cid) if cid else None
    if entry:
        fixed = (entry.get("vectors") or {}).get("collection_name")
        if fixed:
            return fixed
        return f"cfg_{cid}_code"
    from .config import config
    return f"cfg_{config.alias}_code"


def set_vectors(cid: str, points: int, model: str, dimension: int) -> None:
    """Отметить состояние векторов конфигурации (R4/A4): points={"code": N},
    модель, размерность и время обновления — панель и статусные эндпоинты
    читают кэш реестра, не опрашивая Qdrant.
    """
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is not None:
                vectors = entry.setdefault("vectors", {})
                vectors["points"] = {"code": points}
                vectors["model"] = model
                vectors["dimension"] = dimension
                vectors["updated_at"] = datetime_utc()
                save_registry(reg)
    except RegistryBusy:
        logger.warning("set_vectors: реестр занят другим процессом (%s)", cid)


# --------------------------------------------------- миграция legacy-состояния

LEGACY_CONFIG_PATH = Path("/data/config.json")


def migrate_legacy_configs() -> None:
    """Разовая миграция /data/config.json (алиасы) → новый реестр.

    Активная в legacy конфигурация (алиас) маппится на созданный cid:
    default_config_id (settings) и active_default получают ЕГО (R12/A23 —
    раньше в default уходил сырой алиас, которого в реестре нет).
    """
    import secrets as _secrets

    legacy_path = LEGACY_CONFIG_PATH
    if not legacy_path.exists():
        return
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not legacy.get("configs"):
        return

    try:
        with registry_lock():
            reg = load_registry()
            if reg.get("configs"):
                return  # миграция уже выполнена

            migrated: dict[str, str] = {}
            for alias, entry in (legacy.get("configs") or {}).items():
                short = alias.replace("Atlas_", "").split("_")[0][:16] or "CFG"
                uid = _secrets.token_hex(4)
                cid = f"Compass_{short}_{uid}"
                migrated[alias] = cid
                vectors = entry.get("vectors") or {}
                reg.setdefault("configs", {})[cid] = {
                    "short_name": short,
                    "display_name": f"{alias} (мигрировано)",
                    "source_root": entry.get("source_root", ""),
                    "index_db": entry.get("index_db",
                                          f"/data/index/{alias}/index.db"),
                    "mode": entry.get("mode", "fast"),
                    "config_info": entry.get("config_info", {}),
                    "vectors": {"collection_prefix": f"cfg_{alias}",
                                "model": None, "dimension": None,
                                "points": {"code": vectors.get("points",
                                                               {}).get("code",
                                                                       0)}},
                    "created_at": datetime_utc(),
                }
            active_default = migrated.get(legacy.get("active"))
            if active_default:
                reg["active_default"] = active_default
            save_registry(reg)
    except RegistryBusy:
        logger.warning("migrate_legacy_configs: реестр занят другим "
                       "процессом — повтор при следующем старте")
        return
    from . import workspace
    settings = workspace.load_settings()
    settings["default_config_id"] = active_default
    workspace.save_settings(settings)
    legacy_path.rename(legacy_path.with_suffix(".json.migrated"))
    logger.info("Реестр мигрирован: %d конфигураций", len(reg["configs"]))


def build_paths(cid: str) -> dict:
    """Пути сборки по РЕЕСТРУ (F2/pu9): source_root/index_db из записи cid.

    Фоновый поток сборки не наследует contextvar — контекстные
    config.index_db/config.source_root резолвятся по 'panel' и ломают
    первую сборку новой конфигурации (AttributeError: None.parent) или
    собирают не ту базу. env COMPASS_INDEX_DB/COMPASS_SOURCE_ROOT
    приоритетнее (dev-режим).
    Возвращает {cid, source_root, index_db, display_name} или {"error"}.
    """
    import os

    entry = get_config(cid)
    if entry is None:
        return {"error": f"конфигурация '{cid}' не найдена"}
    source_root = Path(os.getenv("COMPASS_SOURCE_ROOT")
                       or entry.get("source_root") or "")
    index_db = Path(os.getenv("COMPASS_INDEX_DB")
                    or entry.get("index_db") or "")
    if not source_root or not source_root.exists():
        return {"error": f"активная выгрузка не задана/нет папки: "
                         f"{source_root or '(пусто)'}"}
    if not (source_root / "Configuration.xml").exists():
        return {"error": (f"в {source_root} нет Configuration.xml — "
                          f"это не выгрузка конфигурации")}
    return {"cid": cid, "source_root": source_root, "index_db": index_db,
            "display_name": entry.get("display_name") or cid}


def move_index_to_volume(cid: str) -> dict:
    """Перенести индекс конфигурации в volume (F9/06k): файл →
    INDEX_ROOT/<cid>/index.db + правка реестра (путь остаётся абсолютным —
    тула/reader без правок). Сборка должна быть завершена; reader-кэш
    сбрасывает вызывающий (роут) после переноса. env-индекс (dev-режим)
    не переносится.
    """
    import shutil
    from . import state

    if state.BUILD_STATE["building"]:
        return {"error": "идёт сборка — перенесите после завершения"}
    entry = get_config(cid)
    if entry is None:
        return {"error": f"конфигурация '{cid}' не найдена"}
    old = Path(entry.get("index_db") or "")
    new = INDEX_ROOT / cid / "index.db"
    if old == new:
        return {"info": "индекс уже в volume"}
    if not old.exists():
        return {"error": f"индекс не найден: {old} — соберите сначала"}
    if new.exists():
        return {"error": f"целевой путь занят: {new}"}
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))  # кросс-устройство = copy+delete
    try:
        with registry_lock():
            reg = load_registry()
            reg["configs"][cid]["index_db"] = str(new)
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"moved": str(old), "to": str(new), "bytes": new.stat().st_size}


def normalize_session_bindings() -> dict:
    """Миграция испорченных привязок sessions.json (F1/compass-zjp).

    До фикса инверсии bind_context привязки писались как
    {chat=<cid> -> config_id='panel'}; также возможны cid, удалённые из
    реестра. Правка: config_id='panel' или отсутствующий в реестре cid →
    подставить settings.default_config_id (если он валиден), иначе удалить
    поле config_id (чат вернётся к дефолту). Валидные привязки и записи
    без config_id не трогаются. Вызывается при старте main().
    """
    from . import workspace
    reg = load_registry()
    configs = reg.get("configs") or {}
    default = workspace.load_settings().get("default_config_id")
    default_ok = bool(default) and default in configs
    fixed: list[str] = []

    def _fix(data: dict) -> None:
        sessions = data.setdefault("sessions", {})
        for chat, rec in list(sessions.items()):
            cid = (rec or {}).get("config_id")
            if not cid:
                continue  # привязки нет — чат и так на дефолте
            if cid != "panel" and cid in configs:
                continue  # валидная привязка
            if default_ok:
                rec["config_id"] = default
            else:
                rec.pop("config_id", None)
            sessions[chat] = rec
            fixed.append(chat)

    workspace.update_sessions(_fix)
    if fixed:
        logger.info("Привязки контекста нормализованы: %s",
                    ", ".join(fixed))
    return {"fixed": sorted(fixed), "default_used": default_ok}


# ------------------------------------------------ инкрементальная пересборка

# Потолок списков файлов в ответе (за ним честный _truncated)
INCREMENTAL_LIST_CAP = 50


def incremental_rebuild(cid: str) -> dict:
    """Точечная пересборка индекса: gyrfalcon update --db <индекс> <файлы…>.

    Сигнатура CLI сверена по vendored/gyrfalcon/crates/gyrfalcon-mcp/
    main.rs cmd_update: --db обязателен; каталог исходников индекс берёт
    из своего index_meta (чужой корпус подсунуть нельзя); принимает
    только .bsl (правка XML — отказ «нужна полная сборка»); удалённые на
    диске файлы убирает из индекса. Diff — GyrReader.changed_modules
    (size/mtime, ddl.rs v0.1.20); новые файлы (не в modules) дифф не
    находит — пометка в ответе.

    Механизм общий с полной сборкой (fc9): build_begin/build_finish +
    set_build_proc — watchdog видит PID gyrfalcon update. Отказы: идёт
    сборка / синк документации (общий index.db) / непустая очередь полных
    сборок (её разгребает только _run_build — инкремент в ней не
    участвует и голодать её не должен).

    Возвращает {"status": "updated"|"fresh", ...} или {"error": ...}.
    """
    import subprocess

    entry = get_config(cid)
    if entry is None:
        return {"error": f"конфигурация '{cid}' не найдена"}
    from . import state
    from .facade.tools import reset_reader
    from .recon.gyr_reader import GyrReader
    from .workspace import load_build_queue

    if state.BUILD_STATE.get("building"):
        return {"error": "идёт сборка — инкремент доступен после её "
                         "завершения"}
    if state.docs_busy():
        return {"error": "идёт синхронизация документации (общий index.db) —"
                         " повторите после неё"}
    queued = load_build_queue()
    if queued:
        return {"error": "очередь полных сборок не пуста: "
                         + ", ".join(queued[:5])
                         + " — инкремент в неё не встаёт, повторите после"}
    paths = build_paths(cid)
    if "error" in paths:
        return paths
    index_db = Path(paths["index_db"])
    if not index_db.exists():
        return {"error": f"индекс не собран: {index_db} — сначала полная "
                         "сборка (панель / POST /reindex)"}
    source_root = Path(paths["source_root"])

    idx = GyrReader(index_db)
    try:
        # index сам помнит свой source_path — расхождение с реестром
        # значило бы дифф не против того дерева, куда update запишет
        index_src = idx.index_meta_value("source_path")
        if index_src and os.path.normcase(
                str(Path(index_src).resolve())) != os.path.normcase(
                str(source_root.resolve())):
            return {"error": "выгрузка реестра не совпадает с source_path "
                             f"индекса ({index_src}) — проверьте запись "
                             "конфигурации"}
        diff = idx.changed_modules(source_root)
    finally:
        idx.close()

    if not diff["changed"] and not diff["deleted"]:
        return {"status": "fresh", "cid": cid,
                "display_name": paths["display_name"],
                "checked": diff["checked"], "unchanged": diff["unchanged"],
                "note": "изменённых .bsl нет — пересборка не нужна"}

    files = [str(source_root / rel)
             for rel in diff["changed"] + diff["deleted"]]
    cmd = ["gyrfalcon", "update", "--db", str(index_db), *files]
    # Симлинк source/ext (S1) — как у полной сборки, идемпотентно
    link = ensure_ext_link(cid)
    if link.get("error"):
        logger.warning("симлинк ext: %s", link["error"])
    gen = state.build_begin()
    tail: list[str] = []
    exit_code = 1
    try:
        # Popen + set_build_proc: PID в BUILD_STATE для watchdog'а (fc9) —
        # как у полной сборки; таймаут тот же 3600 c
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
                    + ["timeout: gyrfalcon update убит по таймауту 3600 c"])
            exit_code = proc.returncode if proc.returncode is not None else 1
    except Exception as exc:
        # падение запуска (нет gyrfalcon в PATH и т.п.) обязано попасть в
        # состояние — building=True не должен зависать (инцидент КА)
        tail = [f"{type(exc).__name__}: {exc}"]
    finally:
        state.build_finish(exit_code, tail, gen)

    if exit_code != 0:
        return {"error": "gyrfalcon update завершился с ошибкой",
                "exit_code": exit_code, "tail": tail[-8:]}

    # сброс читателя: старое соединение держит прежний образ index.db
    reset_reader()
    # indexed_at обновляем; кэш счётчиков (index_counts) не пересчитываем —
    # stats() по drvfs занимает минуты, обновится при полной сборке
    set_indexed(cid)

    # Тела методов (compass-gbi): перезалить изменённые модули; сбой не
    # ломает инкремент — code_grep отдаёт name-only маршрут как раньше
    bodies_report: dict | None = None
    try:
        from .recon.bodies import update_bodies
        bodies_report = update_bodies(index_db, source_root, diff["changed"])
    except Exception as exc:
        logger.warning("тела методов (инкремент): %s", exc)

    out: dict = {
        "status": "updated", "cid": cid,
        "display_name": paths["display_name"],
        "changed_total": len(diff["changed"]),
        "deleted_total": len(diff["deleted"]),
        "unchanged": diff["unchanged"],
        "changed_files": diff["changed"][:INCREMENTAL_LIST_CAP],
        "deleted_files": diff["deleted"][:INCREMENTAL_LIST_CAP],
        "gyrfalcon_tail": tail[-8:],
        "note": "новые файлы (не в modules) дифф не находит — их подберёт "
                "полная сборка; правки XML требуют полной сборки; счётчики "
                "кэша реестра не пересчитаны",
    }
    if len(diff["changed"]) > INCREMENTAL_LIST_CAP or \
            len(diff["deleted"]) > INCREMENTAL_LIST_CAP:
        out["_truncated"] = True
        out["hint"] = f"показаны первые {INCREMENTAL_LIST_CAP} путей"
    if bodies_report is not None:
        out["bodies_fts"] = bodies_report
    if any("ВНИМАНИЕ" in line for line in tail):
        out["stale_edges_hint"] = ("есть вызовы класса unknown, ведущие в "
                                   "переиндексированные модули — разрешатся "
                                   "полной сборкой; семантический поиск "
                                   "новых методов тоже ждёт её")
    return out


# ------------------------------------------------- migrate-to-homes (S4)

def migrate_to_homes(cid: str | None = None) -> dict:
    """Разовый перенос legacy-конфигураций в дома (arch-storage v0.2 §4).

    Для каждой записи реестра (или одной — cid): создать configs/<cid>/,
    ПЕРЕНЕСТИ index.db (если файл есть), СКОПИРОВАТЬ выгрузку в source/
    (источник — текущий source_root; хост-дамп после копии можно
    удалить), перенести help-корпуса из legacy /workspace/Help/<id> в
    docs/<cid>/<id> (список — help_sources индекса), обновить пути,
    ensure_ext_link. Перед стартом — бэкап реестра registry.json.bak-<ts>
    (отката нет, спека §4). После — контрольная сверка: каждый путь
    существует, reader открывает индекс. Reader-кэш сбрасывается (сборка
    после миграции пересоздаст соединение).
    Возвращает {migrated, skipped, errors, checks}.
    """
    import shutil

    from . import workspace as ws
    ids = [cid] if cid else sorted((load_registry().get("configs") or {}))
    reg_path = ws.registry_path()
    backup = reg_path.with_name(
        "registry.json.bak-" + datetime_utc().replace(":", "-"))
    if reg_path.exists():
        shutil.copy2(reg_path, backup)

    migrated: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    checks: dict[str, dict] = {}

    for target in ids:
        entry = get_config(target)
        if entry is None:
            errors.append({"cid": target, "error": "нет в реестре"})
            continue
        home = config_home(target)
        new_source = config_source_root(target)
        new_index = config_index_db(target)
        old_source = Path(entry.get("source_root") or "")
        old_index = Path(entry.get("index_db") or "")
        if old_source.resolve() == new_source.resolve() and                 old_index == new_index:
            skipped.append({"cid": target, "reason": "уже дом-схема"})
            continue

        try:
            home.mkdir(parents=True, exist_ok=True)

            # help-корпуса ДО переноса index.db: список source_id читается
            # из индекса (help_sources), каталоги — из legacy-точки
            help_moved: list[str] = []
            if old_index.exists():
                try:
                    from .recon.gyr_reader import GyrReader
                    reader = GyrReader(old_index)
                    try:
                        source_ids = reader.help_sources()
                    finally:
                        reader.close()
                    for rec in source_ids:
                        sid = (rec or {}).get("source_id") if isinstance(
                            rec, dict) else str(rec)
                        if not sid:
                            continue
                        # legacy точка host-CLI: /workspace/Help/<id>
                        from .docs.crawler import corpus_root as _cr
                        legacy = _cr(sid)
                        dst = config_docs_root(target, sid)
                        if legacy.is_dir() and not dst.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(legacy), str(dst))
                            help_moved.append(sid)
                except Exception as exc:
                    errors.append({"cid": target,
                                   "error": f"help-корпуса: {exc}"})

            # index.db — перенос (_move: та же/другая ФС)
            if old_index.exists():
                if new_index.exists():
                    raise FileExistsError(f"целевой индекс занят: {new_index}")
                new_index.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_index), str(new_index))

            # выгрузка — КОПИЯ (спека §4): хост-дамп остаётся, его можно
            # удалить вручную; symlinks=True — не разыменовывать ext
            if old_source.is_dir() and not new_source.exists():
                shutil.copytree(old_source, new_source, symlinks=True)

            # реестр: дом-пути + свежая версия исходника
            try:
                with registry_lock():
                    reg = load_registry()
                    rec = reg["configs"][target]
                    rec["source_root"] = str(new_source)
                    rec["index_db"] = str(new_index)
                    rec["source_version"] = source_version(new_source)
                    save_registry(reg)
            except RegistryBusy:
                errors.append({"cid": target,
                               "error": "реестр занят другим процессом"})
                continue

            link = ensure_ext_link(target)
            from .facade.tools import reset_reader
            reset_reader()  # старое соединение держало прежний index.db

            # контрольная сверка: пути существуют, reader открывает
            check: dict = {"source": (new_source / "Configuration.xml").exists(),
                           "index": new_index.exists()}
            if new_index.exists():
                try:
                    from .recon.gyr_reader import GyrReader
                    r = GyrReader(new_index)
                    r.close()
                    check["reader_opens"] = True
                except Exception as exc:
                    check["reader_opens"] =                         f"{type(exc).__name__}: {exc}"
            checks[target] = {**check, "help_moved": help_moved,
                              "ext_link": link.get("ok") or link.get(
                                  "skipped") or link.get("error")}
            migrated.append(target)
            logger.info("migrate-to-homes: %s → %s", target, home)
        except Exception as exc:
            errors.append({"cid": target,
                           "error": f"{type(exc).__name__}: {exc}"})

    return {"backup": str(backup) if backup.exists() else None,
            "migrated": migrated, "skipped": skipped, "errors": errors,
            "checks": checks}


# ------------------------------------------------------- стек расширений (Т3)
#
# Порядок применения — свойство ПОДКЛЮЧЕНИЯ базы, в файлах выгрузки его
# нет (arch-t3 v0.5 §3-②). Хранится в entry["stack"]; эффективный порядок
# = сортировка (назначение, позиция в списке конфигуратора) — v0.5 §4.

PURPOSE_RANK = {"Patch": 0, "Customization": 1, "Addition": 2}

# Русские синонимы назначения (как в окне «Расширения конфигуратора»)
PURPOSE_SYNONYMS = {"Исправление": "Patch", "Patch": "Patch",
                    "Адаптация": "Customization", "Customization": "Customization",
                    "Дополнение": "Addition", "Addition": "Addition"}

STACK_STATUSES = ("stable", "dev")


def _stack_of_entry(entry: dict) -> list[dict]:
    return entry.setdefault("stack", [])


def _normalize_purpose(purpose: str) -> str | None:
    """Назначение к канону Patch|Customization|Addition (русские синонимы
    принимаются); None — неизвестное значение."""
    return PURPOSE_SYNONYMS.get((purpose or "").strip().capitalize()
                                or "") or PURPOSE_SYNONYMS.get(
        (purpose or "").strip())


def stack_add(cid: str, name: str, position: int, purpose: str,
              status: str = "stable") -> dict:
    """Добавить расширение в стек конфигурации (директива владельца v0.4:
    position и purpose ОБЯЗАТЕЛЬНЫ — их платформа в файлах не хранит).

    position: 1 = верх списка в конфигураторе (внутри назначения);
    purpose: Patch | Customization | Addition (русские синонимы
    Исправление/Адаптация/Дополнение принимаются); status: stable | dev.
    Возвращает {"added", "name", ...} или {"error": ...}.
    """
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            name = (name or "").strip()
            if not name:
                return {"error": "имя расширения обязательно"}
            try:
                position = int(position)
            except (TypeError, ValueError):
                return {"error": "position — целое число, 1 = верх списка конфигуратора"}
            if position < 1:
                return {"error": "position должна быть ≥ 1 (1 = верх списка "
                                 "конфигуратора)"}
            canon = _normalize_purpose(purpose)
            if canon is None:
                return {"error": f"назначение '{purpose}' неизвестно — используйте "
                                 "Исправление/Patch, Адаптация/Customization, "
                                 "Дополнение/Addition"}
            if status not in STACK_STATUSES:
                return {"error": "status: stable | dev"}
            stack = _stack_of_entry(entry)
            if any(e.get("name") == name for e in stack):
                return {"error": f"расширение '{name}' уже в стеке конфигурации "
                                 "(позицию меняйте через stack_move)"}
            rec = {"name": name, "position": position, "purpose": canon,
                   "status": status, "order_source": "manual"}
            stack.append(rec)
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"added": name, "name": name, "position": position,
            "purpose": canon, "status": status}


def stack_remove(cid: str, name: str) -> dict:
    """Убрать расширение из стека конфигурации (запись реестра; сам
    каталог ext/ и индекс не трогаются)."""
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            stack = _stack_of_entry(entry)
            before = len(stack)
            entry["stack"] = [e for e in stack if e.get("name") != name]
            if len(entry["stack"]) == before:
                return {"error": f"расширение '{name}' не найдено в стеке"}
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"removed": name}


def stack_move(cid: str, name: str, position: int) -> dict:
    """Сменить позицию расширения в списке конфигуратора (внутри своего
    назначения — эффективный порядок пересчитает stack_of)."""
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            try:
                position = int(position)
            except (TypeError, ValueError):
                return {"error": "position — целое число, 1 = верх списка"}
            if position < 1:
                return {"error": "position должна быть ≥ 1"}
            rec = next((e for e in _stack_of_entry(entry)
                        if e.get("name") == name), None)
            if rec is None:
                return {"error": f"расширение '{name}' не найдено в стеке"}
            rec["position"] = position
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"moved": name, "position": position}


def stack_set_status(cid: str, name: str, status: str) -> dict:
    """Переключить статус расширения: stable | dev (dev исключается из
    теней по умолчанию, arch §8)."""
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            if status not in STACK_STATUSES:
                return {"error": "status: stable | dev"}
            rec = next((e for e in _stack_of_entry(entry)
                        if e.get("name") == name), None)
            if rec is None:
                return {"error": f"расширение '{name}' не найдено в стеке"}
            rec["status"] = status
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"updated": name, "status": status}


def stack_of(cid: str) -> list[dict]:
    """Стек в ЭФФЕКТИВНОМ порядке (arch v0.5 §4): сортировка
    (PURPOSE_RANK[purpose], position); каждой записи добавлены
    effective_position (1..N) и order_confirmed (True только если ВСЕ
    записи стека order_source == "runtime")."""
    entry = get_config(cid) or {}
    stack = entry.get("stack") or []
    ordered = sorted(
        stack,
        key=lambda e: (PURPOSE_RANK.get(e.get("purpose"), 99),
                       e.get("position", 0), e.get("name", "")))
    confirmed = bool(ordered) and all(
        e.get("order_source") == "runtime" for e in ordered)
    out = []
    for i, e in enumerate(ordered, start=1):
        out.append({**e, "effective_position": i,
                    "order_confirmed": confirmed})
    return out


def sync_stack_from_ping(cid: str, extensions: list[dict]) -> dict:
    """Пинг-синк стека расширений с живой базой (Ф4, arch §3-L2①).

    extensions — список Пинга [{Имя, Версия, Активно}]. Сверяется СОСТАВ
    и версии: совпавшие — обновление version/synced_at; подключённые в
    базе без регистрации в стеке — missing_in_stack (НЕ добавляются сами
    — директива v0.4: позиция и назначение у разработчика); в стеке, но
    не подключено в базе — missing_in_base; отключённые в базе —
    inactive_in_base. Порядок применения из API платформы недоступен
    (Пинг 0.2.0: ПорядокДоступен=Ложь) — позиции и order_source не
    трогаются (честность §9).
    """
    try:
        with registry_lock():
            reg = load_registry()
            entry = (reg.get("configs") or {}).get(cid)
            if entry is None:
                return {"error": f"конфигурация '{cid}' не найдена"}
            stack = entry.setdefault("stack", [])
            by_name = {}
            for e in stack:
                by_name[(e.get("name") or "").casefold()] = e
            ping_names: set[str] = set()
            matched: list[str] = []
            missing_in_stack: list[dict] = []
            inactive: list[str] = []
            for ext in extensions or []:
                name = str(ext.get("Имя") or "").strip()
                if not name:
                    continue
                key = name.casefold()
                ping_names.add(key)
                rec = by_name.get(key)
                if rec is None:
                    missing_in_stack.append(
                        {"name": name,
                         "version": str(ext.get("Версия") or "")})
                    continue
                version = str(ext.get("Версия") or "")
                if version:
                    rec["version"] = version
                rec["synced_at"] = datetime_utc()
                matched.append(name)
                if not ext.get("Активно"):
                    inactive.append(name)
            missing_in_base = [e.get("name") for e in stack
                               if (e.get("name") or ""
                                   ).casefold() not in ping_names]
            synced_at = datetime_utc()
            entry["stack_synced_at"] = synced_at
            save_registry(reg)
    except RegistryBusy:
        return {"error": "реестр занят другим процессом"}
    return {"cid": cid, "synced_at": synced_at, "matched": matched,
            "missing_in_stack": missing_in_stack,
            "missing_in_base": missing_in_base,
            "inactive_in_base": inactive,
            "order_note": "порядок применения из API платформы недоступен "
                          "(Пинг 0.2.0: ПорядокДоступен=Ложь) — синк "
                          "сверяет состав и версии; позиции подтверждайте "
                          "в конфигураторе"}
