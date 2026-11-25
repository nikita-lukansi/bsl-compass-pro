# -*- coding: utf-8 -*-
"""Семантический слой фасада: codesearch + фоновая векторизация карточек.

Карточки методов строятся из Rust-индекса (имя, параметры, тип модуля,
объект) — та же идея «скелетных карточек», что обкатана в Community
(hit@5 лучше, чем эмбеддинг тел). Коллекция Qdrant — cfg_<алиас>_code.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from ..config import config
from ..semantic.embeddings_client import EmbeddingError, EmbeddingsClient
from ..semantic.hybrid_search import HybridSearch
from ..semantic.indexer import SemanticIndexer
from ..semantic.vector_store import VectorStore
from . import tools

_log = logging.getLogger(__name__)

_sem_lock = threading.Lock()
# _sem["pause"] — событие-РАЗРЕШЕНИЕ работать: set = идём, clear = пауза
# (Event.wait() блокирует на clear — поток индексации замирает до resume)
_sem: dict[str, Any] = {"store": None, "collection": None, "embeddings": None,
                        "indexing": False, "last_stats": None,
                        "pause": threading.Event(),
                        # fa9: {cid, state: warming|warm|cold, error?,
                        # updated_at} — прогрев RAM-копии активной коллекции
                        "warm_state": None,
                        "warming": False,
                        # compass-zi2: анти-шторм автопрогрева — монотонная
                        # метка последнего запуска прогрева (любого: авто
                        # или config_use); и флаг «видели qdrant недоступным»
                        # (dead→alive = рестарт под живым ядром)
                        "last_warm_run_at": 0.0,
                        "qdrant_seen_down": False}
_sem["pause"].set()


def _qdrant_local_path() -> str:
    """Локальное хранилище Qdrant: env QDRANT_STORAGE, дефолт — том
    контейнера /data/qdrant. Читается на вызов (env-override тестов и
    хост-запуска — как COMPASS_* в workspace/registry)."""
    return os.getenv("QDRANT_STORAGE") or "/data/qdrant"


def _collection_name(cid: str | None = None) -> str:
    """Имя коллекции конфигурации — тонкая делегация к
    registry.collection_name (§7 №6, compass-c6z): логика имени живёт
    в реестре, где лежат данные vectors.collection_name; слои (бэкапы)
    зовут публичную точку, приватное имя остаётся внутренним."""
    from ..registry import collection_name
    return collection_name(cid)


def _fix_collection_name(store) -> None:
    """Ленивая фиксация фактического имени коллекции в реестре (R2/A1).

    Порядок (план R2): кандидат cfg_<cid>_code, если жив и непуст;
    иначе легаси cfg_<display_name>_code (точки КА остаются под старым
    именем, но имя теперь записано и переживает переименования);
    иначе кандидат — будущая коллекция.
    """
    from ..registry import get_config, set_collection_name
    cid = config.active_config_id
    if not cid:
        return
    entry = get_config(cid)
    if not entry or (entry.get("vectors") or {}).get("collection_name"):
        return  # уже зафиксировано или записи нет

    def nonempty(name: str) -> bool:
        try:
            return store.collection(name).count() > 0
        except Exception:
            return False  # сервер недоступен = как отсутствие коллекции

    candidate = f"cfg_{cid}_code"
    legacy = f"cfg_{entry.get('display_name') or ''}_code"
    if nonempty(candidate):
        set_collection_name(cid, candidate)
    elif legacy and nonempty(legacy):
        set_collection_name(cid, legacy)
    else:
        set_collection_name(cid, candidate)


def _ensure_stack() -> bool:
    """Ленивая инициализация Qdrant-коллекции и клиента эмбеддингов.

    Параметры клиента — из config.embedding_settings(): settings.json
    переопределяет env, поэтому смена модели из панели подхватывается
    после reset_semantic_stack() без рестарта процесса.

    compass-zi2: первый живой контакт с qdrant в этом процессе (peek
    ответил warm/cold) — точка входа автопрогрева: рестарт qdrant мог
    выгрузить int8-копию из RAM, конфиг при этом лжёт always_ram=True.
    """
    first_contact = False
    with _sem_lock:
        if not config.semantic_ready:
            return False
        if _sem["collection"] is None:
            try:
                emb = config.embedding_settings()
                client = EmbeddingsClient(
                    base_url=emb["base_url"],
                    api_key=emb["api_key"],
                    model=emb["model"],
                )
                # on_disk обязателен (landmine, analysis-semantic §6):
                # без него новые коллекции создаются в cached-режиме с
                # пре-лоадом всего векторного файла в RAM
                store = VectorStore(url=config.qdrant_url or None,
                                    path=_qdrant_local_path(), on_disk=True)
                try:
                    _fix_collection_name(store)
                except Exception:
                    pass  # фиксация реестра best-effort — retry при следующем вызове
                _sem["embeddings"] = client
                _sem["store"] = store
                _sem["collection"] = store.collection(_collection_name())
                if _sem.get("warm_state") is None:
                    # fa9: первый факт о прогреве — из живой коллекции
                    # (best-effort; до этого semantic_status честно unknown)
                    try:
                        _sem["warm_state"] = _peek_warm_state(store)
                    except Exception:
                        _sem["warm_state"] = {"state": "unknown"}
                    # compass-zi2: peek жив (не unknown) = qdrant отвечал —
                    # первый контакт после холодного старта ядра. unknown =
                    # qdrant не отвечал: ждём живого count() в codesearch
                    # (dead→alive там же триггерит автопрогрев)
                    if (_sem["warm_state"] or {}).get("state") == "unknown":
                        _sem["qdrant_seen_down"] = True
                    else:
                        first_contact = True
            except Exception:
                return False
        return_value = _sem["collection"] is not None
    if first_contact:
        _maybe_autowarm("stack_init")
    return return_value


def reset_semantic_stack() -> None:
    """Сбросить ленивый стек (смена эмбеддинг-модели): пересоберётся
    при следующем _ensure_stack(). Идущая индексация не трогается — она
    держит ссылки на старый клиент/коллекцию и дорабатывает с ними."""
    with _sem_lock:
        _sem["store"] = None
        _sem["collection"] = None
        _sem["embeddings"] = None


def _peek_warm_state(store) -> dict:
    """fa9: первый факт о прогреве активной коллекции — живой introspect
    (always_ram в конфиге квантизации). До него semantic_status = unknown."""
    try:
        info = store.client.get_collection(_collection_name())
        ram = bool(getattr(
            getattr(info.config, "quantization_config", None),
            "scalar", None) and info.config.quantization_config
            .scalar.always_ram)
        return {"state": "warm" if ram else "cold",
                "updated_at": _utc_now()}
    except Exception:
        return {"state": "unknown"}


def _utc_now() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime()) + "Z"


def _run_warmup(prev_cid: str | None, new_cid: str | None) -> dict:
    """fa9: синхронное тело прогрева (поток — schedule_warmup).

    Порядок: СНАЧАЛА выгрузка прежней (always_ram=False — RAM
    освобождается до загрузки новой, пик потребления = одна коллекция),
    ПОТОМ прогрев новой (True). Каждый шаг — свой VectorStore (стек _sem
    мог быть сброшен reset_runtime_caches); коллекция по registry
    (collection_name переживает переименования). Пропуски честные: нет
    конфигурации в реестре, нет квантизации, no-op. Итог — в
    _sem["warm_state"]: warming → warm/cold (+error при сбое).
    """
    from ..registry import collection_name, get_config
    from ..semantic.vector_store import VectorStore, set_always_ram

    steps: list[dict] = []

    def _apply(cid: str | None, flag: bool, label: str,
               force: bool = False) -> None:
        if not cid:
            return
        entry = get_config(cid)
        if entry is None:
            steps.append({"step": label, "skipped": "нет в реестре"})
            return
        try:
            store = VectorStore(url=config.qdrant_url or None,
                                path=_qdrant_local_path(), on_disk=True)
            res = set_always_ram(store.client, collection_name(cid), flag,
                                 force=force)
        except Exception as exc:
            res = {"error": f"{type(exc).__name__}: {exc}"}
        steps.append({"step": label, "config_id": cid, **res})

    _apply(prev_cid, False, "evict_previous")
    with _sem_lock:
        _sem["warm_state"] = {"cid": new_cid, "state": "warming",
                              "updated_at": _utc_now()}
    # compass-zi2: прогрев активной — форсированный повторный enable:
    # если qdrant рестартовал между переключениями, конфиг уже
    # always_ram=True, а int8-копия в RAM холодная (no-op пропуск
    # оставил бы коллекцию холодной)
    _apply(new_cid, True, "warm_active", force=True)
    result: dict[str, Any] = {"steps": steps}
    last = steps[-1] if steps else {}
    state = "warm" if last.get("updated") or last.get("skipped") else "cold"
    if any("error" in s for s in steps):
        state = "cold"
        result["error"] = next(s["error"] for s in steps if "error" in s)
    result["state"] = state
    with _sem_lock:
        _sem["warm_state"] = {"cid": new_cid, "state": state,
                              "updated_at": _utc_now(),
                              **({"error": result["error"]}
                                 if "error" in result else {})}
    return result


def schedule_warmup(prev_cid: str | None, new_cid: str | None) -> dict:
    """fa9: неблокирующий прогрев после config_use — PATCH always_ram
    (выгрузка прежней, прогрев новой) в daemon-потоке.

    SEMANTIC_WARM=off — exotic-режим «все в RAM»: переключения не нужны,
    честный skip. Уже идущий прогрев не дублируется. Возвращает
    {"state": "warming", ...} | {"skipped"/"info": ...}; прогресс —
    semantic_status.warm_state."""
    if os.getenv("SEMANTIC_WARM", "").strip().lower() == "off":
        return {"skipped": "SEMANTIC_WARM=off — все коллекции в RAM, "
                           "переключение не требуется"}
    with _sem_lock:
        if _sem.get("warming"):
            return {"info": "прогрев уже идёт (semantic_status.warm_state)",
                    "warm_state": _sem.get("warm_state") or {}}
        _sem["warming"] = True
        # compass-zi2: осознанный прогрев занимает окно анти-шторма —
        # автопрогреву сразу после config_use нечего добавлять
        _sem["last_warm_run_at"] = time.monotonic()
    cid_fixed = new_cid

    def _wrap() -> None:
        try:
            _run_warmup(prev_cid, cid_fixed)
        finally:
            with _sem_lock:
                _sem["warming"] = False

    threading.Thread(target=_wrap, daemon=True,
                     name="semantic-warmup").start()
    with _sem_lock:
        state = {"cid": cid_fixed, "state": "warming",
                 "updated_at": _utc_now()}
        _sem["warm_state"] = state
    return {"state": "warming", "config_id": cid_fixed,
            "previous": prev_cid}


# compass-zi2: анти-шторм автопрогрева — минимальный интервал между
# запусками (сек, env SEMANTIC_AUTOWARM_INTERVAL; повторный cold в окне
# НЕ должен ре-патчить qdrant). Читается на вызов — env-override тестов.
AUTOWARM_DEFAULT_INTERVAL = 900.0


def _autowarm_interval() -> float:
    """Эффективный интервал автопрогрева (сек); кривой env — дефолт."""
    raw = os.getenv("SEMANTIC_AUTOWARM_INTERVAL", "")
    try:
        return float(raw) if raw.strip() else AUTOWARM_DEFAULT_INTERVAL
    except ValueError:
        _log.warning("SEMANTIC_AUTOWARM_INTERVAL=%r не число — дефолт %s",
                     raw, AUTOWARM_DEFAULT_INTERVAL)
        return AUTOWARM_DEFAULT_INTERVAL


def _maybe_autowarm(reason: str) -> None:
    """compass-zi2: точка входа автопрогрева — лёгкая неблокирующая
    проверка (реестр из кэша, без сети), при соблюдении условий —
    фоновый поток _run_auto_warm. Триггеры: первый живой контакт с
    qdrant после холодного старта ядра (stack_init) и dead→alive
    переход по count() в codesearch (qdrant_back — рестарт под живым
    ядром).

    Условия (все — иначе тихий skip): SEMANTIC_WARM≠off; активная
    конфигурация с векторами (points>0, чужие/пустые не трогаем);
    прогрев не идёт; с последнего запуска прошло ≥ интервала
    (штамп ставит и осознанный config_use-прогрев). Сетевых вызовов
    здесь нет — точка висит на пути codesearch/semantic_status."""
    if os.getenv("SEMANTIC_WARM", "").strip().lower() == "off":
        return
    try:
        from ..registry import get_config
        cid = config.active_config_id
        entry = (get_config(cid) if cid else None) or {}
        points = (((entry.get("vectors") or {}).get("points") or {})
                  .get("code")) or 0
    except Exception as exc:
        _log.warning("автопрогрев (%s): реестр недоступен — %s: %s",
                     reason, type(exc).__name__, exc)
        return
    if not cid or points <= 0:
        return  # нет активной конфигурации с векторами — греть нечего
    now = time.monotonic()
    with _sem_lock:
        if _sem.get("warming"):
            return
        if now - _sem.get("last_warm_run_at", 0.0) < _autowarm_interval():
            return
        _sem["last_warm_run_at"] = now
        _sem["warming"] = True

    def _wrap() -> None:
        try:
            _run_auto_warm(cid, reason)
        finally:
            with _sem_lock:
                _sem["warming"] = False

    threading.Thread(target=_wrap, daemon=True,
                     name="semantic-autowarm").start()


def _run_auto_warm(cid: str, reason: str) -> None:
    """compass-zi2: тело автопрогрева — форсированный повторный enable
    int8 (set_always_ram force=True) активной коллекции. После рестарта
    qdrant конфиг always_ram=True персистит (introspect лжёт «warm»), а
    физическая int8-копия в RAM поднимается до ~10 мин — повторный enable
    заставляет оптимизатор поднять квантование (лечение инцидента 17.11).
    Свой VectorStore (как _run_warmup — стек _sem мог быть сброшен).
    Итог — в _sem["warm_state"]: warming → warm/cold (+error при сбое)."""
    from ..registry import collection_name
    from ..semantic.vector_store import VectorStore, set_always_ram

    _log.info("автопрогрев int8 запущен (%s): cid=%s", reason, cid)
    with _sem_lock:
        _sem["warm_state"] = {"cid": cid, "state": "warming",
                              "updated_at": _utc_now(),
                              "reason": reason}
    result: dict[str, Any]
    try:
        store = VectorStore(url=config.qdrant_url or None,
                            path=_qdrant_local_path(), on_disk=True)
        result = set_always_ram(store.client, collection_name(cid), True,
                                force=True)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    state = ("warm" if (result.get("updated") or result.get("skipped"))
             else "cold")
    with _sem_lock:
        _sem["warm_state"] = {
            "cid": cid, "state": state, "updated_at": _utc_now(),
            "reason": reason,
            **({"error": result["error"]} if "error" in result else {})}
    _log.info("автопрогрев int8 (%s) завершён: cid=%s state=%s %s",
              reason, cid, state, result)


# compass-yho: поллинг прогрева для config_use(wait_warm=true) — интервал
# опроса и потолок ожидания (сверх — честная отмена, без исключений)
WARM_WAIT_INTERVAL = 0.2
WARM_WAIT_TIMEOUT = 60.0


def wait_warm(timeout: float = WARM_WAIT_TIMEOUT) -> dict:
    """Дождаться завершения прогрева RAM-копии активной коллекции
    (compass-yho, обратка №5: «тёпл-состояние непрозрачно»).

    Источник статуса — тот же, что у semantic_status.warm_state:
    поллинг _sem["warm_state"], новых механизмов нет. warming → ждём,
    прочие состояния (warm/cold/unknown) — ответ сразу. Возвращает
    {"state": ..., "waited": True}; по таймауту — текущее состояние
    с timeout: True (честная отмена, исключений нет)."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with _sem_lock:
            state = (_sem.get("warm_state") or {}).get("state", "unknown")
        if state != "warming":
            return {"state": state, "waited": True}
        if time.monotonic() >= deadline:
            return {"state": state, "waited": True, "timeout": True}
        time.sleep(WARM_WAIT_INTERVAL)


def semantic_status() -> dict:
    """Состояние семантического слоя (панель и тулы).

    base_url/source — эффективные параметры без api_key (префилл плитки
    настроек); source показывает, что действует: settings.json или env.
    paused — пауза оператора (индексация висит на Event, деньги не тратятся).
    """
    emb = config.embedding_settings()
    # fa9: прогрев RAM-копии активной коллекции — warming/warm/cold
    # (unknown — факта ещё нет: стек не поднимался)
    warm_state = (_sem.get("warm_state") or {}).get("state", "unknown")
    out: dict[str, Any] = {
        "configured": config.semantic_ready,
        "model": emb["model"],
        "base_url": emb["base_url"],
        "source": emb["source"],
        "collection": _collection_name(),
        "indexing": _sem["indexing"],
        "paused": not _sem["pause"].is_set(),
        "last_stats": _sem["last_stats"],  # всегда, не только при сбое стека (R4/A5)
        "warm_state": warm_state,
        # compass-yho: краткий булев итог для агента — коллекция прогрета
        # в RAM (codesearch без холодного старта); cold — поиск работает,
        # но первый запрос медленнее; согласован с warm_state
        "ready": warm_state == "warm",
    }
    points = None
    if config.semantic_ready and _ensure_stack():
        # PERF-3/compass-ehr: points — из кэша реестра (set_vectors пишет
        # его по завершении прогона; docstring set_vectors прямо это
        # предписывает). Живой exact-count к Qdrant из статусного пути
        # убран: /api/status дёргает semantic_status в event loop, и
        # сетевой count там блокировал панель.
        from ..registry import get_config
        cid = config.active_config_id
        entry = (get_config(cid) if cid else None) or {}
        points = ((entry.get("vectors") or {}).get("points") or {}).get("code")
    if points is None:
        points = _sem["last_stats"]["indexed"] if _sem["last_stats"] else 0
    out["points"] = points
    return out


def _iter_method_documents(reader):
    """Генератор карточек методов постранично (id-пагинация) — память
    ограничена страницей, вся конфигурация (227k методов) не материуется."""
    last_id = 0
    while True:
        rows = reader.methods_page(last_id=last_id, limit=5000)
        if not rows:
            return
        for r in rows:
            yield {
                "source": r["rel_path"],
                "name": r["name"],
                "text": f"Метод {r['name']}{r['params'] or ''} — "
                        f"{r['module_type']} {r['object_name']}",
                "metadata": {"name": r["name"], "full_path": r["rel_path"],
                             "module_type": r["module_type"],
                             "object_name": r["object_name"],
                             "is_export": bool(r["is_export"])},
            }
        last_id = rows[-1]["id"]


def _run_indexing(cid: str | None = None) -> None:
    """Фоновая векторизация: карточки → эмбеддинги → Qdrant (resume).

    cid — фиксация активной конфигурации на старте потока (contextvar в
    потоке не виден): по завершении успеха/частичного прогона состояние
    векторов пишется в реестр (R4/A4).
    """
    try:
        reader = tools.reader()
        indexer = SemanticIndexer(_sem["collection"], _sem["embeddings"],
                                  log=lambda s: None,
                                  pause_event=_sem["pause"])
        _sem["last_stats"] = indexer.index_iterable(_iter_method_documents(reader))
        from ..registry import set_vectors
        from ..workspace import datetime_utc
        try:
            dim = _sem["collection"].dimension()
        except Exception:
            dim = 0
        set_vectors(cid, _sem["collection"].count(),
                    config.embedding_settings()["model"], dim)
        _sem["last_stats"]["finished_at"] = datetime_utc()
    except Exception as exc:
        _sem["last_stats"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        _sem["indexing"] = False
        _sem["pause"].set()  # сброс паузы по завершении прогона


def start_semantic_index() -> dict:
    """Запуск фоновой векторизации (кнопка панели / тул)."""
    if not tools.reader():
        return {"error": "сначала соберите структурный индекс"}
    if not _ensure_stack():
        return {"error": "EMBEDDING_BASE_URL/EMBEDDING_MODEL не заданы"}
    if _sem["indexing"]:
        return {"info": "векторизация уже идёт"}
    _sem["indexing"] = True
    _sem["pause"].set()  # новый прогон стартует не в паузе
    cid = config.active_config_id  # фиксируем ДО старта потока (contextvar!)
    threading.Thread(target=_run_indexing, args=(cid,), daemon=True,
                     name="semantic-index").start()
    return {"status": "started", "collection": _collection_name()}


def pause_semantic_index() -> dict:
    """Пауза оператора: поток индексации замирает перед следующим батчем
    (эмбеддинги не вызываются — расход останавливается)."""
    if not _sem["indexing"]:
        return {"info": "векторизация не идёт"}
    _sem["pause"].clear()
    return {"paused": True}


def resume_semantic_index() -> dict:
    """Снять паузу — поток продолжит со следующего батча."""
    was = not _sem["pause"].is_set()
    _sem["pause"].set()
    return {"resumed": was, "indexing": _sem["indexing"]}


# compass-sm4: режимы семантики конфигурации (решение владельца 16.11,
# подтверждено техлидом 23.11): full = семантика включена, fast = выключена.
# Точки в Qdrant НИКОГДА не удаляются (повторная векторизация платная):
# fast — это honest-отказ codesearch + сгрузка RAM-копии активной коллекции.


def _maybe_start_vectorize(config_id: str, confirm: bool) -> dict:
    """compass-sm4: запуск векторизации при переходе конфигурации в full.

    Платный внешний API — БЕЗ confirm никогда: только подсказка. С confirm
    — старт только для активной конфигурации контекста: start_semantic_index
    векторизует именно её; для прочих — честный маршрут переключения."""
    if not confirm:
        return {"skipped": "векторизация не запущена (confirm=False): "
                           f"запустите векторизацию конфигурации {config_id} "
                           "(панель/воркер) или повторите с confirm=True"}
    if config_id != config.active_config_id:
        return {"skipped": "конфигурация не активна в этом контексте — "
                           "сначала config_use(...), затем semantic_mode(..., "
                           "confirm=True)"}
    return start_semantic_index()


def _evict_ram_copy(config_id: str) -> dict:
    """compass-sm4: сгрузка RAM-копии коллекции (fa9-механика) при переходе
    конфигурации в fast.

    Греется (и тратит RAM) только активная коллекция — неактивную не
    трогаем, честный skip. SEMANTIC_WARM=off — exotic-режим «все в RAM»
    (как в schedule_warmup): сгрузка не требуется. Точки НЕ удаляются:
    always_ram=False лишь выгоняет int8-копию из RAM, on-disk остаётся.
    Сбой сгрузки не роняет переключение (режим уже в реестре) — честная
    пометка; при успехе warm_state честно гасится в cold."""
    if os.getenv("SEMANTIC_WARM", "").strip().lower() == "off":
        return {"skipped": "SEMANTIC_WARM=off — все коллекции в RAM, "
                           "сгрузка не требуется"}
    if config_id != config.active_config_id:
        return {"skipped": "не активная конфигурация — RAM-копия не "
                           "прогревается, сгрузка не требуется"}
    from ..registry import collection_name
    from ..semantic.vector_store import VectorStore, set_always_ram
    try:
        store = VectorStore(url=config.qdrant_url or None,
                            path=_qdrant_local_path(), on_disk=True)
        result = set_always_ram(store.client, collection_name(config_id),
                                False)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if "error" not in result:
        with _sem_lock:
            _sem["warm_state"] = {"cid": config_id, "state": "cold",
                                  "updated_at": _utc_now(),
                                  "reason": "semantic_mode_fast"}
    return result


def semantic_mode(config_id: str, mode: str, confirm: bool = False) -> dict:
    """compass-sm4: переключить режим семантики конфигурации в реестре.

    full = семантика включена; fast = выключена (honest-отказ codesearch,
    сгрузка RAM-копии активной коллекции; точки в Qdrant сохраняются).

    Параметры:
        config_id — ID конфигурации в реестре;
        mode — "fast" | "full";
        confirm — разрешение на ПЛАТНУЮ векторизацию при переходе в full:
            без него — только флаг в реестре и подсказка о запуске.

    Возвращаемое значение: {"config_id", "previous", "mode", ...} с
    деталями веток: fast→full — "vectorize" (skipped-подсказка либо
    результат start_semantic_index при confirm); full→fast — "ram"
    (итог сгрузки always_ram) и "info" о сохранённых точках. Отказы
    честные: кривой mode, неизвестный config_id, занятый реестр."""
    if mode not in ("fast", "full"):
        return {"error": "mode: fast|full"}
    from ..registry import all_configs, get_config
    entry = get_config(config_id)
    if entry is None:
        known = [c["id"] for c in all_configs()]
        return {"error": f"конфигурация '{config_id}' не найдена",
                "known": known}
    previous = entry.get("mode", "fast")
    if previous == mode:
        return {"info": f"конфигурация уже в режиме {mode}",
                "config_id": config_id, "mode": mode}
    from ..registry import update_config
    res = update_config(config_id, mode=mode)
    if "error" in res:
        return res
    out: dict[str, Any] = {"config_id": config_id, "previous": previous,
                           "mode": mode}
    if mode == "full":
        out["vectorize"] = _maybe_start_vectorize(config_id, confirm)
    else:
        out["ram"] = _evict_ram_copy(config_id)
        out["info"] = ("семантика выключена: codesearch честно откажет с "
                       "подсказкой; точки в Qdrant сохранены (повторная "
                       "векторизация платная)")
    return out


def codesearch(query: str, limit: int = 10) -> list[dict] | dict:
    """Семантический поиск по методам (вектор + буквальный через RRF).

    compass-sm4: fast = семантика ВЫКЛЮЧЕНА для активной конфигурации —
    honest-инфо с маршрутом включения (не ошибка); точки при этом в
    Qdrant сохранены, повторное включение векторизации не требует."""
    if not tools.reader():
        return {"error": "сначала соберите структурный индекс"}
    # compass-sm4: режим активной конфигурации (решение владельца 16.11);
    # проблемы реестра ветку не роняют (паттерн _config_badge), без
    # активной конфигурации проверки нет — дальше старые честные ветки
    cid = config.active_config_id
    if cid:
        try:
            from ..registry import get_config
            entry = get_config(cid) or {}
        except Exception:
            entry = {}
        if entry.get("mode", "fast") == "fast":
            info = {"info": f"семантика выключена режимом fast конфигурации "
                            f"{cid}; semantic_mode(..., \"full\") чтобы "
                            "включить",
                    "mode": "fast"}
            info.update(tools._config_badge())
            return info
    if not _ensure_stack():
        return {"error": "семантика не настроена: EMBEDDING_BASE_URL/EMBEDDING_MODEL"}
    try:
        points = _sem["collection"].count()
    except Exception as exc:
        # compass-50a: сбой Qdrant НЕ «индекс пуст» — контракты count()
        # (упавший сервер отличим от пустой коллекции) и честных отказов
        # compass-zi2: недоступность фиксируем — возврат к жизни
        # (успешный count) = вероятный рестарт qdrant → автопрогрев
        with _sem_lock:
            _sem["qdrant_seen_down"] = True
        return {"error": f"Qdrant не ответил: {type(exc).__name__}: {exc} — "
                         "проверьте QDRANT_URL/сервис и повторите; "
                         "векторизация не нужна"}
    with _sem_lock:
        was_down = _sem.get("qdrant_seen_down", False)
        _sem["qdrant_seen_down"] = False
    if was_down:
        # dead→alive под живым ядром: int8-копия могла остыть — фоново
        _maybe_autowarm("qdrant_back")
    if not points:
        info = {"info": "векторный индекс пуст — запустите векторизацию "
                        "(кнопка на панели или тул start_semantic_index)",
                # compass-yho: агент видит, ЧТО пусто (корень инцидента
                # обратки №5 — чужой контекст без эха config); points —
                # живой count() этой ветки (здесь всегда 0), сбой Qdrant
                # сюда не попадает — это контракт compass-50a выше
                "points": points}
        info.update(tools._config_badge())
        # compass-50a: расхождение реестр↔Qdrant не молчим — в реестре
        # вектора есть, а коллекция пуста/нет (смена модели, имя, сервер)
        from ..registry import get_config
        entry = (get_config(config.active_config_id)
                 if config.active_config_id else None) or {}
        registry_points = ((entry.get("vectors") or {}).get("points")
                           or {}).get("code")
        if registry_points:
            info["discrepancy"] = (f"в реестре points={registry_points}, "
                                   f"в Qdrant коллекция "
                                   f"{_collection_name()} пуста/отсутствует — "
                                   "проверьте сервер и коллекцию "
                                   "(semantic_status)")
        return info
    hs = HybridSearch(collection=_sem["collection"], embeddings=_sem["embeddings"])
    # F8/xsv: запросный путь с сокращённым таймаутом — недоступный/подвисший
    # провайдер не должен держать клиента MCP ~135 с (timeout + ретраи)
    import os
    q_timeout = float(os.getenv("SEMANTIC_QUERY_TIMEOUT", "15"))
    try:
        rows = hs.search(query, limit=limit, query_timeout=q_timeout)
    except EmbeddingError as exc:
        return {"error": f"провайдер эмбеддингов не ответил за {q_timeout:g} c "
                         f"— проверьте semantic_status и повторите ({exc})"}
    return [{"name": r["name"], "full_path": r["full_path"],
             "module_type": r["metadata"].get("module_type", ""),
             "score": r["score"]} for r in rows]
