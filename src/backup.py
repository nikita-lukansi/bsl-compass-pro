# -*- coding: utf-8 -*-
"""Движок бэкапов: слепок конфигурации = индекс + манифест (+ снапшот Qdrant).

Формат (каталог, не архив — по согласованной концепции #30):
    <Backups-hub>/<алиас>/<алиас>_<UTC-время>/
        manifest.json   контракт переноса между компасами
        index.db        консистентная копия (VACUUM INTO — без остановки читателей)
        qdrant.snapshot снапшот семантики (если настроена; сбой — помета в манифесте)
Ротация: хранятся последние BACKUP_KEEP слепков (дефолт 3).
Inbox: принесённый извне каталог с manifest.json; после успешного restore
переносится в Backups-hub (не удаляется) — пополнение архива.

Хабы по умолчанию — системная зона volume /data/system (S3: концепция
рабочего каталога WORKSPACE_HUB выпилена, слепки в volume компаса).
COMPASS_BACKUPS_PATH/COMPASS_INBOX_PATH — override тестов и хоста.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.request

from .config import APP_VERSION, config

# S3 (arch-storage v0.2 §2): фиксированная системная зона /data/system;
# env COMPASS_*_PATH — override для тестов и хост-запуска
BACKUPS_HUB = Path(os.getenv("COMPASS_BACKUPS_PATH", "/data/system/backups"))
INBOX_HUB = Path(os.getenv("COMPASS_INBOX_PATH", "/data/system/inbox"))
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "3")))


def _ensure_within(path: Path, *hubs: Path) -> Path:
    """Каталог внутри одного из хабов (аудит SAFE-4): resolve + relative_to;
    ValueError — путь вне хабов бэкапов."""
    resolved = path.resolve()
    for hub in hubs:
        try:
            resolved.relative_to(hub.resolve())
            return resolved
        except ValueError:
            continue
    raise ValueError(f"путь вне хабов бэкапов: {path}")


def _manifest(index_db: Path, alias: str, extra: dict | None = None) -> dict:
    """Манифест слепка: версии, счётчики индекса, время, источник."""
    counts: dict[str, int] = {}
    error: str | None = None
    try:
        conn = sqlite3.connect(f"file:{index_db.as_posix()}?mode=ro", uri=True)
        for table in ("methods", "modules", "calls", "object_attributes"):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        error = f"{type(exc).__name__}: {exc}"
    manifest: dict[str, Any] = {
        "compass_version": APP_VERSION,
        "alias": alias,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index_counts": counts,
    }
    if error:
        manifest["index_error"] = error
    if extra:
        manifest.update(extra)
    return manifest


def _vacuum_into(source: Path, target: Path) -> None:
    """Консистентная копия живого индекса (без остановки читателей)."""
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def _qdrant_snapshot(collection: str, target: Path) -> str | None:
    """Скачать снапшот коллекции Qdrant (если сервер доступен).

    Таймауты — env (F7/compass-vlr, коллекции до десятков ГБ):
    QDRANT_SNAPSHOT_CREATE_TIMEOUT (дефолт 600 с, было 120),
    QDRANT_SNAPSHOT_DOWNLOAD_TIMEOUT (дефолт 900 с, было 300 — запись
    через drvfs медленная).
    """
    if not config.qdrant_url:
        return None
    base = config.qdrant_url.rstrip("/")
    t_create = int(os.getenv("QDRANT_SNAPSHOT_CREATE_TIMEOUT", "600"))
    t_download = int(os.getenv("QDRANT_SNAPSHOT_DOWNLOAD_TIMEOUT", "900"))
    try:
        req = urllib.request.Request(
            f"{base}/collections/{collection}/snapshots", method="POST")
        with urllib.request.urlopen(req, timeout=t_create) as resp:
            data = json.loads(resp.read().decode())
        name = data["result"]["name"]
        with urllib.request.urlopen(
                f"{base}/collections/{collection}/snapshots/{name}/download",
                timeout=t_download) as resp, open(target, "wb") as f:
            shutil.copyfileobj(resp, f)
        return name  # файл остаётся в слепке: переносимость между компасами
    except Exception:
        return None


def restore_semantic(cid: str, dir_name: str) -> dict:
    """Восстановить семантику из слепка: снапшот → Qdrant (F7/vlr).

    Qdrant REST: POST /collections/{name}/snapshots/upload?priority=snapshot
    (multipart, поле snapshot) — клиент qdrant-client 1.20 метода upload
    не имеет (проверено), поэтому прямая HTTP-загрузка urllib. Целевая
    коллекция — иммутабельное имя из реестра (registry.collection_name, R2);
    манифест слепка — сверка соответствия (qdrant_collection, с этого
    коммита пишется в манифест; старые слепки — честное предупреждение).
    """
    import uuid

    from .registry import collection_name
    source = None
    for child in sorted(BACKUPS_HUB.glob(f"*/{dir_name}")):
        if (child / "manifest.json").is_file():
            source = child
            break
    if source is None:
        return {"error": f"слепок '{dir_name}' не найден в {BACKUPS_HUB}"}
    try:
        manifest = json.loads(
            (source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"манифест битый: {exc}"}
    snapshot = source / "qdrant.snapshot"
    if not snapshot.exists():
        return {"error": "в слепке нет qdrant.snapshot (семантика не "
                         "снапшотилась при создании)"}
    from .config import config
    if not config.qdrant_url:
        return {"error": "QDRANT_URL не задан"}
    expected = manifest.get("qdrant_collection")
    target_collection = collection_name(cid)
    note = ""
    if expected and expected != target_collection:
        return {"error": (f"манифест слепка ссылается на коллекцию "
                          f"'{expected}', целевая — '{target_collection}' "
                          "(иммутабельное имя реестра)") }
    if not expected:
        note = ("в манифесте слепка нет имени коллекции (старый слепок) — "
                "целевая по реестру")

    base = config.qdrant_url.rstrip("/")
    boundary = uuid.uuid4().hex
    crlf = "\r\n"
    body = (f"--{boundary}{crlf}"
            'Content-Disposition: form-data; name="snapshot"; '
            'filename="qdrant.snapshot"' + crlf +
            "Content-Type: application/octet-stream" + crlf + crlf).encode()
    body += snapshot.read_bytes()
    body += (crlf + f"--{boundary}--" + crlf).encode()
    url = (f"{base}/collections/{target_collection}/snapshots/upload"
           "?priority=snapshot")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(
                req, timeout=int(os.getenv(
                    "QDRANT_SNAPSHOT_DOWNLOAD_TIMEOUT", "900"))) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": f"upload снапшота не удался: {exc}"}
    out = {"restored": target_collection, "dir": str(source),
           "qdrant": result}
    if note:
        out["note"] = note
    return out


def rotate(alias: str, keep: int | None = None) -> int:
    """Оставить последние keep слепков конфигурации, вернуть число удалённых.

    keep разрешается в вызове (не в дефолте) — дефолт из env должен
    читаться актуальным значением, а не связанным при импорте.
    """
    keep = keep if keep is not None else BACKUP_KEEP
    root = BACKUPS_HUB / alias
    if not root.is_dir():
        return 0
    snaps = sorted(p for p in root.iterdir() if p.is_dir())
    stale = snaps[:-keep] if len(snaps) > keep else []
    for p in stale:
        shutil.rmtree(p, ignore_errors=True)
    return len(stale)


def create_backup(cid: str | None, alias: str, index_db: Path) -> dict:
    """Создать слепок конфигурации; вернуть отчёт.

    cid — для имени Qdrant-коллекции из реестра (R2/A1, иммутабельное);
    None — легаси-имя по алиасу. Сбой любой части не должен ронять
    caller'а: отчёт несёт ошибки.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = BACKUPS_HUB / alias / f"{alias}_{stamp}"
    # уникализация: несколько слепков в одну секунду не конфликтуют
    # (VACUUM INTO отказывает, если целевой файл уже существует)
    seq = 0
    while target_dir.exists():
        seq += 1
        target_dir = BACKUPS_HUB / alias / f"{alias}_{stamp}-{seq}"
    target_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"dir": str(target_dir)}

    db_copy = target_dir / "index.db"
    try:
        _vacuum_into(index_db, db_copy)
        report["index_db_bytes"] = db_copy.stat().st_size
    except sqlite3.Error as exc:
        report["error"] = f"VACUUM INTO не удался: {exc}"
        return report

    qdrant_name = None
    try:
        try:
            from .registry import collection_name
            qdrant_collection = collection_name(cid)
        except Exception:
            qdrant_collection = f"cfg_{alias}_code"
        qdrant_name = _qdrant_snapshot(qdrant_collection,
                                       target_dir / "qdrant.snapshot")
    except Exception:
        qdrant_name = None
    manifest = _manifest(db_copy, alias,
                         {"qdrant_snapshot": qdrant_name,
                          "qdrant_collection": qdrant_collection,
                          "config_id": cid})
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report.update({"created": True, "manifest": manifest,
                   "rotated": rotate(alias)})
    return report


# ------------------------------------------------------------------- inbox

def scan_inbox() -> list[dict]:
    """Каталоги /inbox с валидным manifest.json — кандидаты на восстановление."""
    out: list[dict] = []
    if not INBOX_HUB.is_dir():
        return out
    for child in sorted(INBOX_HUB.iterdir()):
        mf = child / "manifest.json"
        if not (child.is_dir() and mf.exists()):
            continue
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            out.append({"dir": child.name, "alias": manifest.get("alias", "?"),
                        "created_at": manifest.get("created_at", "?"),
                        "has_index": (child / "index.db").exists()})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def restore_backup(source_dir: Path, alias: str | None = None) -> dict:
    """Восстановить слепок: index.db на место + регистрация в реестре.

    Единый реестровый путь (A2/P10): существующий short_name — индекс
    заменяется на месте (восстановление поверх); новый — конфигурация
    добавляется registry.add_config, индекс кладётся в реестровый
    /workspace/data/SQLite/<cid>/index.db (legacy /data/index/<алиас>/
    больше не используется). Деструктивно подменяет индекс; отказ при
    идущей сборке или алиасе вне правил short_name.
    """
    source_dir = _ensure_within(source_dir, BACKUPS_HUB, INBOX_HUB)
    from . import registry as registry_module
    from . import state
    from .facade import registry_tools

    mf_path = source_dir / "manifest.json"
    if not mf_path.exists():
        return {"error": f"manifest.json не найден: {source_dir}"}
    try:
        manifest = json.loads(mf_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"error": f"манифест битый: {exc}"}
    if not (source_dir / "index.db").exists():
        return {"error": "index.db отсутствует в слепке"}

    if state.BUILD_STATE["building"]:
        return {"error": "идёт сборка — повторите после завершения"}

    alias = alias or manifest.get("alias") or source_dir.name.split("_")[0]
    existing = None
    for c in registry_module.all_configs():
        if c.get("short_name") == alias:
            existing = c
            break
    manifest_cid = manifest.get("config_id")
    if existing is not None and manifest_cid and manifest_cid != existing["id"]:
        return {"error": f"слепок другой конфигурации ({manifest.get('alias')!r},"
                         f" config_id {manifest_cid}) — отказ, индекс {existing['id']} не тронут"}
    if existing is not None:
        cid = existing["id"]
        index_db = Path(existing["index_db"])
    else:
        # выгрузки-источника в слепке нет (manifest его не хранит) —
        # source_root пуст, пересборка для такой конфигурации честно откажет
        result = registry_module.add_config(
            alias, manifest.get("source_root", ""))
        if "error" in result:
            return result
        cid = result["id"]
        index_db = Path(result["entry"]["index_db"])
    index_db.parent.mkdir(parents=True, exist_ok=True)
    staging = index_db.with_suffix(".restoring")
    shutil.copy2(source_dir / "index.db", staging)
    registry_tools.reset_runtime_caches()  # сброс до замены файла
    staging.replace(index_db)

    from .context import bind_default, bind_context, PANEL_CONTEXT
    bind_default(cid)
    bind_context(cid, PANEL_CONTEXT)
    registry_tools.reset_runtime_caches()
    return {"restored": alias, "id": cid, "index_db": str(index_db),
            "manifest": manifest}


def apply_inbox_backup(dir_name: str) -> dict:
    """Restore из /inbox с переносом исходника в архив /backups (концепция #30)."""
    source = INBOX_HUB / dir_name
    if not source.is_dir():
        return {"error": f"каталог не найден в inbox: {dir_name}"}
    result = restore_backup(source)
    if "restored" not in result:
        return result  # отказ — исходник не трогаем
    alias = result["restored"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = BACKUPS_HUB / alias / f"{alias}_{stamp}_inbox"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        shutil.move(str(item), archive_dir / item.name)
    source.rmdir()
    result["archived_to"] = str(archive_dir)
    result["rotated"] = rotate(alias)
    return result
