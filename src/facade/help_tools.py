# -*- coding: utf-8 -*-
"""Тулы корпуса документации ИТС: help_search / help_read / help_status /
help_sync / help_remove.

Корпус лежит в workspace/Help/<source_id>/** и индексируется
`gyrfalcon kb sync` в таблицы help_pages/help_fts того же index.db
(multi-source: справка выгрузки — source_id="primary", ИТС-корпус —
ka25doc). Фоновая синхронизация: фетчер (docs.crawler.run_crawl) →
kb sync; пауза оператора — Event (клиент замирает перед следующим
запросом, сеть не трогается); гейт на идущую сборку индекса в обе
стороны (kb sync пишет в тот же index.db, что и gyrfalcon build).

Протокол использования агентом — в MCP-инструкциях main.py (§0 спецификации).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import state
from ..config import config
from ..docs.crawler import corpus_root, run_crawl
from ..docs.its_client import ItsClient, ItsError
from ..docs.mentions import find_mentions
from . import tools

logger = logging.getLogger(__name__)

_KB_TIMEOUT = 3600      # kb sync по всем файлам корпуса: минуты, запас — час
_FETCH_TIMEOUT = 7200   # полный обход книги ~45 мин на антибот-профиле

# source_id становится путём (хаб → дом): только латиница/цифры/дефис/подчерк
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _source_id_error(source_id: str) -> str | None:
    """Честный отказ для source_id (P2-4, аудит 4l5), иначе None.

    source_id становится частью пути корпуса (docs/<cid>/<source_id>),
    поэтому все входы — help_sync, help_remove, import_corpus — валидируют
    его одинаково: латиница/цифры/дефис/подчерк, до 64 символов."""
    sid = (source_id or "").strip()
    if not _SOURCE_ID_RE.fullmatch(sid):
        return (f"source_id '{source_id}' недопустим — латиница/цифры/"
                "дефис/подчерк, до 64 символов (становится частью пути "
                "корпуса)")
    return None


def docs_hub_root() -> Path:
    """Корень хаба готовых help-корпусов: env DOCS_HUB, если путь
    существует ЗДЕСЬ (host-run), иначе маунт-пойнт /docs-dumps
    (compass-85s; bcl: compose пробрасывает host-значения для панели —
    в контейнере они не существуют). Приём — import_corpus/help_import."""
    val = os.getenv("DOCS_HUB")
    if val and Path(val).is_dir():
        return Path(val)
    return Path("/docs-dumps")

_docs_lock = threading.Lock()
# _docs["pause"] — событие-РАЗРЕШЕНИЕ работать: set = идём, clear = пауза
_docs: dict[str, Any] = {
    "fetching": False,
    "pause": threading.Event(),
    "last_stats": None,
    "names_cache": {},  # db_path → (moment, set имён) для mentions
}
_docs["pause"].set()


def _names(reader) -> set[str]:
    """Кэш имён объектов для mentions (TTL 300 с на индекс)."""
    key = str(tools.active_index_db())
    now = time.monotonic()
    cached = _docs["names_cache"].get(key)
    if cached and now - cached[0] < 300:
        return cached[1]
    names = set(reader.object_names())
    _docs["names_cache"][key] = (now, names)
    return names


def help_search(query: str, limit: int = 10, source_id: str | None = None):
    """Полнотекстовый поиск по документации ИТС (FTS5 по title/object_name/text).

    Возвращает страницы {source_id, path, title, snippet, mentions}.
    `mentions` — имена объектов метаданных, встретившиеся в тексте
    (мост «дока ↔ код»: далее verify_field/read_function по ним).
    Каждый ответ несёт config активной конфигурации (compass-yho), когда
    она выбрана: dict — ключом, list — завершающим элементом-маркером.
    """
    reader = tools.reader()
    if not reader:
        return tools._echo_config({"error": "сначала соберите структурный "
                                            "индекс"})
    try:
        rows = reader.help_search(query, source_id=source_id, limit=limit)
    except sqlite3.OperationalError:
        # любой сбой FTS-синтаксиса (unterminated string и т.п.) — честное «нет»
        return tools._echo_config(
            {"_no_matches": True,
             "hint": "запрос не разобран FTS5: уберите спецсимволы "
                     "или используйте голые слова / префикс «парти*»"})
    except Exception as exc:
        return tools._echo_config({"error": f"{type(exc).__name__}: {exc}"})
    if not rows:
        return tools._echo_config(
            {"_no_matches": True,
             "hint": "FTS5 не знает форм слова («серии» ≠ «серия») — "
                     "перебирайте формы слова или используй префикс "
                     "«парти*»; поиск по title/object_name/text"})
    names = _names(reader)
    for row in rows:
        row["mentions"] = find_mentions(row.get("snippet") or "", names)
    return tools._echo_config(rows)


def help_read(source_id: str, path: str):
    """Прочитать страницу документации целиком (source_id + path из help_search).

    Возвращает {title, category, object_name, text, mentions} — text
    очищен от навигации ИТС ещё при выгрузке.
    """
    reader = tools.reader()
    if not reader:
        return {"error": "сначала соберите структурный индекс"}
    try:
        rows = reader.help_page(source_id, path)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not rows:
        return {"_no_matches": True,
                "hint": "страницы нет в корпусе: проверьте source_id/path "
                        "(список источников — help_status)"}
    page = rows[0]
    page["mentions"] = find_mentions(page.get("text") or "", _names(reader))
    return page


def help_status() -> dict:
    """Состояние корпуса документации: источники, счётчики, синхронизация.

    configured — источник кредов («settings»/«env»/«none») БЕЗ секрета;
    «none» = обновление недоступно, поиск/чтение по индексу работают.
    """
    out: dict[str, Any] = {
        "configured": config.its_credentials()["source"],
        "fetching": _docs["fetching"],
        "paused": not _docs["pause"].is_set(),
        "last_stats": _docs["last_stats"],
    }
    reader = tools.reader()
    if not reader:
        out["sources"] = []
        return out
    try:
        out["sources"] = reader.help_sources()
    except Exception:
        # в этом index.db ещё нет help-таблиц — корпус не синхронизирован
        out["sources"] = []
        out["sources_note"] = "help-таблиц нет: запустите help_sync"
    return out


def sync_corpus(source_id: str, cid: str | None = None,
                db: Any = None, pause_event: Any = None) -> dict:
    """Синхронная выгрузка корпуса ИТС + kb sync (CLI manage, S5).

    cid — корпус в docs-хаб конфигурации (docs/<cid>/<source_id>) и
    запись в ЕЁ index.db (db — путь, дефолт: активный индекс контекста).
    MCP-тул help_sync продолжает фоновый путь через _run_sync.
    """
    creds = config.its_credentials()
    client = ItsClient(login=creds["login"], password=creds["password"],
                       cookie=creds["cookie"], delay=creds["delay"],
                       pause_event=pause_event)
    client.ensure_session()
    stats: dict[str, Any] = run_crawl(
        client, source_id, root=corpus_root(source_id, cid),
        log=lambda msg: logger.info("[help-sync] %s", msg))
    target_db = db or tools.active_index_db()
    if not target_db:
        raise ItsError("нет активного индекса — kb sync некуда писать")
    cmd = ["gyrfalcon", "kb", "sync", "--db", str(target_db),
           "--source-id", source_id,
           "--src", str(corpus_root(source_id, cid))]
    logger.info("[help-sync] kb sync: %d файлов...",
                stats.get("files_fetched", 0))
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=_KB_TIMEOUT)
    stats["kb_sync"] = {
        "exit_code": proc.returncode,
        "tail": (proc.stdout or proc.stderr or "").strip().splitlines()[-5:],
    }
    stats["ok"] = proc.returncode == 0
    return stats


def _run_sync(source_id: str) -> None:
    """Фоновая синхронизация: sync_corpus в потоке. Любой исход — last_stats."""
    stats: dict[str, Any]
    try:
        stats = sync_corpus(source_id, pause_event=_docs["pause"])
    except Exception as exc:
        stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if isinstance(stats, dict):
            stats["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                                 time.gmtime()) + "Z"
        _docs["last_stats"] = stats
        _docs["fetching"] = False
        state.DOCS_BUSY.clear()  # публичный хук для реестра (§7 №5, compass-c6z)
        _docs["pause"].set()  # сброс паузы по завершении прогона
        logger.info("[help-sync] готово: ok=%s", stats.get("ok"))


def help_sync(source_id: str = "ka25doc") -> dict:
    """Выгрузить/обновить корпус документации ИТС и синхронизировать индекс.

    Фон: логин → дерево оглавления → файлы глав → нарезка страниц →
    `gyrfalcon kb sync`. Прогресс/итог — help_status. При идущей сборке
    индекса — отказ (kb sync пишет в тот же index.db).
    P2-4: source_id валидируется до любых действий (становится путём).
    """
    err = _source_id_error(source_id)
    if err:
        return {"error": err}
    source_id = (source_id or "").strip()
    if _docs["fetching"]:
        return {"info": "синхронизация документации уже идёт (help_status)"}
    if state.BUILD_STATE["building"]:
        return {"error": "идёт пересборка индекса — help_sync пишет в тот же "
                         "index.db; повторите после сборки"}
    if not tools.reader():
        return {"error": "сначала соберите структурный индекс"}
    creds = config.its_credentials()
    if creds["source"] == "none":
        return {"error": "обновление недоступно: нет кредов ИТС "
                         "(ITS_LOGIN/ITS_PASSWORD или ITS_COOKIE); поиск и "
                         "чтение по уже выгруженному корпусу работают"}
    with _docs_lock:
        if _docs["fetching"]:  # двойная проверка под локом
            return {"info": "синхронизация документации уже идёт"}
        _docs["fetching"] = True
        state.DOCS_BUSY.set()  # публичный хук для реестра (§7 №5, compass-c6z)
    _docs["pause"].set()  # новый прогон стартует не в паузе
    threading.Thread(target=_run_sync, args=(source_id,), daemon=True,
                     name="help-sync").start()
    return {"status": "started", "source_id": source_id,
            "note": "прогресс — help_status; пауза — help_sync_pause"}


def help_sync_pause() -> dict:
    """Пауза выгрузки: клиент замирает перед следующим запросом (сеть молчит)."""
    if not _docs["fetching"]:
        return {"info": "выгрузка не идёт"}
    _docs["pause"].clear()
    return {"paused": True}


def help_sync_resume() -> dict:
    """Продолжить выгрузку после паузы."""
    was = not _docs["pause"].is_set()
    _docs["pause"].set()
    return {"resumed": was, "fetching": _docs["fetching"]}


def help_remove(source_id: str = "ka25doc") -> dict:
    """Отвязать help-источник от индекса (`gyrfalcon kb remove`) — remedy.

    Сценарии: смена выгрузки конфигурации («индекс Help принадлежит
    другому проекту»), пересборка корпуса с нуля, чужой source_id.
    Файлы корпуса на диске не удаляются — повторный help_sync поднимет
    источник заново.
    P2-4: source_id валидируется до запуска gyrfalcon (становится путём).
    """
    err = _source_id_error(source_id)
    if err:
        return {"error": err}
    source_id = (source_id or "").strip()
    if _docs["fetching"]:
        return {"error": "идёт синхронизация — дождитесь завершения"}
    db = tools.active_index_db()
    if not db:
        return {"error": "нет активного индекса"}
    cmd = ["gyrfalcon", "kb", "remove", "--db", str(db),
           "--source-id", source_id]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=600)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"exit_code": proc.returncode,
                           "source_id": source_id}
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    if tail:
        out["report"] = tail[-5:]
    return out


def import_corpus(source_id: str, cid: str = "") -> dict:
    """Приём ГОТОВОГО корпуса из хаба DOCS_HUB без ИТС-кредов (compass-85s).

    Копия /docs-dumps/<source_id>/ (env DOCS_HUB) → дом конфигурации
    corpus_root(source_id, cid) = docs/<cid>/<source_id> + локальный
    `gyrfalcon kb sync --db <индекс> --source-id <id> --src <дом>` — путь
    sync_corpus без ItsClient: файлы уже выгружены (машина с ИТС-доступом
    или перенос между машинами), сеть не трогается.

    cid пуст — активная конфигурация контекста (нет — честный отказ).
    Гейты как у kb-пути: идёт сборка индекса / выгрузка (kb sync пишет в
    тот же index.db) / нет активного индекса. Дом корпуса уже занят —
    отказ (молча перезаписывать нельзя). source_id — только
    латиница/цифры/дефис/подчерк (становится путём).
    """
    source_id = (source_id or "").strip()
    err = _source_id_error(source_id)
    if err:
        return {"error": err}
    if _docs["fetching"]:
        return {"error": "идёт синхронизация документации — повторите после"}
    if state.BUILD_STATE.get("building") or state.docs_busy():
        return {"error": "идёт пересборка индекса — kb sync пишет в тот же "
                         "index.db; повторите после сборки"}
    from ..context import active_config_id
    target_cid = (cid or "").strip() or active_config_id()
    if not target_cid:
        return {"error": "конфигурация не выбрана — задайте cid или "
                         "config_use"}
    target_db = tools.active_index_db()
    if not target_db:
        return {"error": "нет активного индекса — соберите структурный "
                         "индекс (kb sync некуда писать)"}
    hub_src = docs_hub_root() / source_id
    if not hub_src.is_dir():
        return {"error": f"корпуса нет в хабе: {hub_src} (DOCS_HUB — "
                         f"{docs_hub_root()}); выгрузите корпус на машине "
                         "с ИТС-доступом или проверьте source_id"}
    if not any(hub_src.rglob("*")):
        return {"error": f"корпус в хабе пуст: {hub_src}"}
    dst = corpus_root(source_id, target_cid)
    if dst.exists() and any(dst.iterdir()):
        return {"error": f"дом корпуса занят: {dst} — обновление делайте "
                         "после удаления/переименования, молча "
                         "перезаписывать нельзя"}
    import shutil
    try:
        shutil.copytree(hub_src, dst)
    except OSError as exc:
        return {"error": f"копия {hub_src} → {dst} не удалась: "
                         f"{type(exc).__name__}: {exc}"}
    cmd = ["gyrfalcon", "kb", "sync", "--db", str(target_db),
           "--source-id", source_id, "--src", str(dst)]
    logger.info("[help-import] kb sync: %s", dst)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_KB_TIMEOUT)
        kb = {"exit_code": proc.returncode,
              "tail": (proc.stdout or proc.stderr or "")
              .strip().splitlines()[-5:]}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"imported": str(hub_src), "to": str(dst),
                "error": f"kb sync не выполнен: {type(exc).__name__}: {exc} "
                         "(копия корпуса на месте — повторный help_import "
                         "после очистки дома продолжит с kb sync)"}
    return {"imported": str(hub_src), "to": str(dst),
            "kb_sync": kb, "ok": kb["exit_code"] == 0}


def help_import(source_id: str, cid: str = "") -> dict:
    """Приём готового корпуса документации из DOCS_HUB без ИТС-кредов.

    Корпус уже выгружен в хаб (/docs-dumps/<source_id>/, read-only
    маунт — машина с ИТС-доступом или перенос между машинами): копия в
    дом docs/<cid>/<source_id> + локальный gyrfalcon kb sync. Сеть не
    трогается. cid пуст — активная конфигурация контекста.
    """
    return import_corpus(source_id, cid)
