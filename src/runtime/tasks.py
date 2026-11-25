# -*- coding: utf-8 -*-
"""Реестр офлайн-задач: статусы, watcher, восстановление (arch §9).

Протокол файлов (arch §4, финализация заявок — arch §11):
queue/req_<id>.json, results/resp_<id>.json,
archive/req_<id>.{taken,json.done,json.broken},
archive/resp_<id>.{loaded,broken}.
Форма 1С финализирует заявку на месте: .json.done после успешной записи
resp, .json.broken — битый пакет (arch §11); resp_<id>.broken — карантин
невалидного ответа (не JSON-объект/битая кодировка — решение watcher'а);
уборка терминальных файлов из queue/ и quarantine — на watcher'е
(перенос в archive/, там досматривает TTL-чистка). Источник истины при
рестарте — файлы; журнал — только история (в v1 не пишем).

Статус-машина: queued (req записан) → in_1c (.taken увиден) →
ready (resp распарсен, per-chat анонимизация, resp → archive) →
delivered (wait выдал; идемпотентно). Ветви: error («Фатально» в resp),
expired (_EXPIRE_SEC без resp; поздний resp воскрешает в ready),
cancelled (cancel из queued). Результаты важнее TTL (compass-5pw):
ответ, доставленный после expired — из results/ или archive/ —
воскрешает задачу, вид задачи помечается late: true; expired без
ответа остаётся честным отказом. Ошибки отдельных вызовов внутри
пакета — это ready с ok=false, не статус error (частичная ошибка —
норма).

Результаты в памяти — анонимизированные (per-chat словарь чата задачи):
агент видит токены, ГУИДы остаются на стороне 1С. Маскировка — сквозная,
на уровне сериализации ответа (compass-lz7): все строковые значения
answer → токены, кроме whitelist (числа/булевы, ISO-даты, ГУИДы, пустые);
ошибки ok:false → {code, masked_message} с классом кода (P1). TTL памяти
_RESULT_TTL; долговечная копия — archive/resp_<id>.loaded (переанонимизация
по wait-запросу, токены после рестарта будут новыми).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import offline
from .anonymizer import TokenAnonymizer, get_anonymizer

logger = logging.getLogger(__name__)

STATUSES = ("queued", "in_1c", "ready", "delivered",
            "error", "expired", "cancelled")

_WATCH_INTERVAL = float(os.getenv("WATCHER_INTERVAL", "2"))
_EXPIRE_SEC = float(os.getenv("TASK_EXPIRE_TIMEOUT", "3600"))
_RESULT_TTL = float(os.getenv("TASK_RESULT_TTL", "1200"))
_ARCHIVE_TTL = float(os.getenv("ARCHIVE_TTL", str(7 * 24 * 3600)))

# Честная подсказка агенту на expired (compass-5pw): отказ не окончательный
_EXPIRED_NOTE = ("TTL истёк без ответа; если владелец выполнит обмен позже, "
                 "повторный offline_wait доставит результаты "
                 "(в виде задачи — пометка late: true)")

# Валидация submit: пакет ≤100 вызовов {method, params}
_MAX_CALLS = 100

# Имена файлов очереди (роут скачивания — защита от path-traversal)
_QUEUE_NAME_RE = re.compile(r"req_[0-9a-z]+\.(?:json|taken|done)")


def safe_queue_name(name: str) -> bool:
    """Имя файла очереди безопасно: только req_<id>.[json|taken|done],
    без путей и посторонних расширений (роут GET /api/offline/requests)."""
    return bool(_QUEUE_NAME_RE.fullmatch(name or ""))


def _archive_raw_on() -> bool:
    """ARCHIVE_RAW: хранить ли сырые (неанонимизированные) ответы в archive/
    (читается при каждом использовании). off — плата: переанонимизация и
    owner-деанон из архива недоступны — честные отказы (arch §2, ревью)."""
    return os.getenv("ARCHIVE_RAW", "on").lower() in ("", "on", "1", "true")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_ts(iso: str | None) -> float:
    """ISO-строка → epoch-секунды; битая/отсутствующая → 0.0 (считается
    «не указано», не взрывается на старых файлах)."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def deanonymize_params(params: Any, chat: str) -> Any:
    """Токены per-chat словаря → исходные значения (ГУИД/ссылки) при
    сборке req: 1С получает реальные ключи, LLM их не видит. Рекурсивно;
    неизвестные строки остаются как есть (решение Б, arch §4)."""
    a = get_anonymizer(chat)
    if isinstance(params, dict):
        return {k: deanonymize_params(v, chat) for k, v in params.items()}
    if isinstance(params, list):
        return [deanonymize_params(x, chat) for x in params]
    if isinstance(params, str):
        return a.deanonymize_value(params) or params
    return params


# ------------------------------------------------- сериализация ответа (compass-lz7)
#
# Точка подготовки results (arch §9, до записи в память и owner-вида):
# P0 — сквозная маскировка всех строковых значений ответа (anonymize_response),
# закрывает 6 каналов утечки (представления ссылок, разыменования через точку,
# Код/НомераГТД, владельцы упаковок); P1 — структурированные коды ошибок
# ok:false → {code, masked_message}: класс классифицируется по НЕзамаскированному
# тексту и не маскируется, сам текст — токеном как сейчас.

# Классы ошибок пакета (P1): по ядровым текстам ошибок 1С/обработки,
# классификация в BSL-обработке не нужна.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # RLS/права: «нет прав», «недостаточно прав», «права доступа»,
    # «отказ в доступе», платформенные тексты RLS (регистронезависимо)
    ("ACCESS_DENIED",
     re.compile(r"нет прав|недостаточн\w* прав|права? доступа|"
                r"отказ в доступе|\bRLS\b", re.IGNORECASE)),
    ("FIELD_NOT_FOUND",
     re.compile(r"поле не обнаружено|поле объекта не обнаружено|"
                r"не найдено|не входит в выборку", re.IGNORECASE)),
    ("SYNTAX_ERROR", re.compile(r"синтаксическ", re.IGNORECASE)),
)


def _classify_error(text: str) -> str:
    """Класс ошибки по тексту: ACCESS_DENIED | FIELD_NOT_FOUND |
    SYNTAX_ERROR | OTHER. Порядок паттернов значим: «объект не найден
    (удалён или нет прав)» — ACCESS_DENIED, а не OTHER."""
    for code, pattern in _ERROR_PATTERNS:
        if pattern.search(text or ""):
            return code
    return "OTHER"


def _prepare_call_result(item: Any, a: TokenAnonymizer) -> Any:
    """Один вызов пакета → вид агента. ok:false → {method, ok, code,
    masked_message} (P1); иначе {method, ok, answer} со сквозной маскировкой
    answer (P0). method/ok — служебные, не маскируются."""
    if not isinstance(item, dict):
        return a.anonymize_response(item)
    out = {"method": item.get("method"), "ok": item.get("ok")}
    answer = item.get("answer")
    if out["ok"] is False:
        err_text = ""
        if isinstance(answer, dict):
            err_text = str(answer.get("Ошибка") or "")
        elif isinstance(answer, str):
            err_text = answer
        out["code"] = _classify_error(err_text)
        out["masked_message"] = a.anonymize_value(err_text)
        return out
    out["answer"] = a.anonymize_response(answer)
    return out


def _prepare_results(results: Any, a: TokenAnonymizer) -> Any:
    """Массив results → вид агента (P0+P1, см. анонс блока выше)."""
    if not isinstance(results, list):
        return a.anonymize_response(results)
    return [_prepare_call_result(item, a) for item in results]


def _prepare_fatal(data: dict, a: TokenAnonymizer) -> tuple[str | None, str]:
    """«Фатально» пакета → (текст-токен, класс) — тоже строковое значение
    ответа (P0) и ошибка (P1). Пустое → (None, "OTHER")."""
    fatal_text = str(data.get("Фатально") or "")
    if not fatal_text:
        return None, "OTHER"
    return a.anonymize_value(fatal_text), _classify_error(fatal_text)


@dataclass
class Task:
    id: str
    chat: str
    description: str
    calls: list[dict]
    status: str = "queued"
    created_at: str = field(default_factory=_now_iso)
    transitions: dict = field(default_factory=dict)   # статус → время
    results: list | None = None        # АНОНИМИЗИРОВАННЫЕ (per-chat)
    fatal: str | None = None           # «Фатально» из resp (текст-токен)
    fatal_code: str | None = None      # класс «Фатально» (P1, compass-lz7)
    unrecognized: bool = False         # осиротевший resp без задачи


class TaskRegistry:
    """Один на процесс; watcher — daemon-поток тика _WATCH_INTERVAL.

    После рестарта Task.calls восстанавливаются из req-файла — там
    деанонимизированные params (тот вид, что ушла 1С); до рестарта —
    токеновый вид как пришло от агента.
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else offline.OFFLINE_ROOT
        offline.ensure_dirs(self.root)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_base: dict | None = None   # {name, version} последнего resp
        self._restore()

    # --- публичное API (вызывают тулы/REST) ---

    def runtime_base(self) -> dict | None:
        """{name, version} из последнего resp (Пинг.Версия/Синоним,
        compass-5vq): бейдж версии рантайма для runtime_status. None —
        обменов ещё не было (честная неопределённость, не догадка)."""
        with self._lock:
            return dict(self._last_base) if self._last_base else None

    def submit(self, chat: str, calls: list[dict], description: str = "") -> Task:
        """Создать задачу: валидация пакета, атомарная запись req в queue/.

        calls: список ≤100 {method: непустая строка, params: dict}.
        Params в req-файле деанонимизируются словарём чата (токен →
        исходное значение, решение Б); Task.calls — токеновый вид.
        Возвращает Task в статусе queued. ValueError — невалидный пакет
        (файл при этом не пишется).
        """
        if not isinstance(calls, list) or not (1 <= len(calls) <= _MAX_CALLS):
            raise ValueError(
                f"calls: непустой список вызовов, максимум {_MAX_CALLS}")
        for c in calls:
            if (not isinstance(c, dict)
                    or not isinstance(c.get("method"), str)
                    or not c["method"].strip()
                    or not isinstance(c.get("params"), dict)):
                raise ValueError(
                    "каждый вызов — {method: непустая строка, params: dict}")
        chat = (chat or "panel").strip()[:100] or "panel"
        description = str(description or "")
        with self._lock:
            task_id = "t" + secrets.token_hex(4)
            while (task_id in self._tasks
                   or (self.root / "queue" / f"req_{task_id}.json").exists()):
                task_id = "t" + secrets.token_hex(4)
            created_at = _now_iso()
            # В файл — деанонимизированные params (их увидит 1С); в памяти
            # Task.calls — токеновый вид как пришло от агента
            req_calls = [{"method": c["method"],
                          "params": deanonymize_params(c["params"], chat)}
                         for c in calls]
            req = {"version": 1, "task_id": task_id, "chat": chat,
                   "created_at": created_at, "description": description,
                   "calls": req_calls}
            # Файл — источник истины: пишем ДО регистрации в памяти
            # (упало здесь — задачи нет; упало после — restore подхватит)
            offline.write_atomic(
                self.root / "queue" / f"req_{task_id}.json",
                json.dumps(req, ensure_ascii=False))
            task = Task(id=task_id, chat=chat, description=description,
                        calls=calls, status="queued", created_at=created_at,
                        transitions={"queued": created_at})
            self._tasks[task_id] = task
            return task

    def wait(self, chat: str, task_id: str, timeout: float = 120.0) -> dict:
        """Дождаться результата задачи (короткие циклы 0.5 с, ≤ timeout с).

        ready/delivered → task_view + статус delivered (идемпотентно —
        повторный wait выдаёт снова, пока результат доступен);
        error/expired/cancelled → view с этим статусом (expired с ответом
        в archive/ воскрешается на месте — результаты важнее TTL,
        compass-5pw); таймаут → {"status": "timeout"}. Чужой/неизвестный
        task_id → {"error", "my_tasks": живые задачи своего чата}.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"error": f"задача {task_id} не найдена",
                        "my_tasks": self._live_ids(chat)}
            if task.chat != chat:
                return {"error": f"задача {task_id} принадлежит другому чату",
                        "my_tasks": self._live_ids(chat)}
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                status = task.status
                if status in ("ready", "delivered"):
                    return self._deliver(task)
                if status in ("error", "expired", "cancelled"):
                    if status == "expired" and self._revive_from_archive(task):
                        return self._deliver(task)
                    out = self._view(task)
                    if status == "expired":
                        out["_note"] = _EXPIRED_NOTE
                    return out
            if time.time() >= deadline:
                return {"status": "timeout", "task_id": task_id,
                        "hint": "задача ещё не готова; повторите "
                                "offline_wait(task_id) — выдача идемпотентна"}
            time.sleep(0.5)

    def cancel(self, chat: str, task_id: str, owner: bool = False) -> dict:
        """Отменить задачу из queued: удалить req из очереди, статус
        cancelled. Готовые/взятые — честный отказ. owner=True (панель —
        владелец): ограничение чата снимается."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or (not owner and task.chat != chat):
                return {"error": f"задача {task_id} не найдена в чате {chat}"}
            if task.status != "queued":
                return {"error": f"отмена невозможна: статус {task.status}; "
                                 "отменить можно только queued"}
            for name in (f"req_{task_id}.json", f"req_{task_id}.taken"):
                try:
                    (self.root / "queue" / name).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("cancel: не удалён %s: %s", name, exc)
            task.status = "cancelled"
            task.transitions["cancelled"] = _now_iso()
            return self._view(task)

    def list(self, chat: str | None = None, status: str | None = None,
             owner: bool = False) -> list[dict]:
        """Список задач (обратная хронология) с фильтрами чат/статус;
        chat=None — все чаты. owner=True — owner-вид каждой задачи (панель)."""
        with self._lock:
            tasks = [t for t in self._tasks.values()
                     if (chat is None or t.chat == chat)
                     and (status is None or t.status == status)]
            return [self._view(t, owner=owner) for t in
                    sorted(tasks, key=lambda t: t.created_at, reverse=True)]

    def resubmit(self, chat: str, task_id: str, owner: bool = False) -> dict:
        """Переотправка (панель): новый submit с теми же calls/description
        (новый task_id), старая задача → cancelled; req из очереди удаляется,
        чтобы 1С не исполнила отменённое. owner=True — доступ без проверки
        чата (чат задачи сохраняется)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or (not owner and task.chat != chat):
                return {"error": f"задача {task_id} не найдена в чате {chat}"}
            calls, description, task_chat = task.calls, task.description, task.chat
            for name in (f"req_{task_id}.json", f"req_{task_id}.taken"):
                try:
                    (self.root / "queue" / name).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("resubmit: не удалён %s: %s", name, exc)
            task.status = "cancelled"
            task.transitions["cancelled"] = _now_iso()
            new_task = self.submit(task_chat, calls, description)
            return {"resubmitted": new_task.id}

    def task_view(self, chat: str, task_id: str, owner: bool = False) -> dict | None:
        """Вид одной задачи. owner=True (панель — владелец): доступ без
        проверки чата + results_note (v1: деанонимизация результатов —
        фаза 2 панели, панель видит токены и честную пометку)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if not owner and task.chat != chat:
                return None
            return self._view(task, owner=owner)

    def status_view(self, chat: str, task_id: str) -> dict:
        """Лёгкий вид задачи для агента без ожидания (compass-5vq):
        статус/переходы/наличие результата, БЕЗ results — за ними
        offline_wait. Контракт отказа — как у wait: {"error",
        "my_tasks": живые задачи чата} при чужом/неизвестном id."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.chat != chat:
                return {"error": f"задача {task_id} не найдена в чате {chat}",
                        "my_tasks": self._live_ids(chat)}
            return {"id": task.id, "status": task.status,
                    "description": task.description,
                    "created_at": task.created_at,
                    "transitions": dict(task.transitions),
                    "calls_methods": [c.get("method") for c in task.calls
                                      if isinstance(c, dict)],
                    "has_results": task.results is not None,
                    "fatal": task.fatal, "fatal_code": task.fatal_code}

    # --- внутреннее ---

    def _view(self, task: Task, owner: bool = False) -> dict:
        """Словарь задачи: методы вызовов (имена, не params — params
        остаются на стороне 1С/файла), переходы с временами, результаты.

        owner=True (панель — владелец, Т2-I): результаты деанонимизированы
        словарём чата задачи; при пустой памяти — сырой ответ из
        archive/*.loaded (results_source: "archive_raw"); нигде — None
        с честной пометкой. Агентский вид не меняется: токены."""
        view = {
            "id": task.id,
            "chat": task.chat,
            "description": task.description,
            "status": task.status,
            "created_at": task.created_at,
            "transitions": dict(task.transitions),
            "calls_methods": [c.get("method") for c in task.calls
                              if isinstance(c, dict)],
            "results": task.results,
            "fatal": task.fatal,
            "fatal_code": task.fatal_code,
            "unrecognized": task.unrecognized,
        }
        if ("expired" in task.transitions
                and task.status in ("ready", "delivered", "error")):
            view["late"] = True   # ответ пришёл после TTL (compass-5pw)
        if owner:
            if task.results is not None:
                view["results"] = get_anonymizer(task.chat).deanonymize_data(
                    task.results)
            else:
                raw = None
                loaded = self.root / "archive" / f"resp_{task.id}.loaded"
                if loaded.exists():
                    try:
                        data = json.loads(loaded.read_text(encoding="utf-8-sig"))
                        raw = data.get("results")
                        view["results_source"] = "archive_raw"
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.warning("owner-view: архив %s не прочитан: %s",
                                       loaded.name, exc)
                view["results"] = raw
                if raw is None:
                    view["results_note"] = (
                        "результат утерян (ARCHIVE_RAW=off)"
                        if not _archive_raw_on()
                        else "результат истёк из памяти (TTL), в архиве отсутствует")
        return view

    def _live_ids(self, chat: str) -> list[str]:
        """Живые задачи чата (подсказка агенту при ошибке чата/id)."""
        return [t.id for t in self._tasks.values()
                if t.chat == chat and t.status in ("queued", "in_1c", "ready")]

    def _deliver(self, task: Task) -> dict:
        """Выдача результата: ready → delivered; при пустой памяти —
        переанонимизация из archive/*.loaded (токены после рестарта
        будут новыми — честная пометка _note)."""
        note = None
        if task.results is None:
            loaded = self.root / "archive" / f"resp_{task.id}.loaded"
            if loaded.exists():
                try:
                    data = json.loads(loaded.read_text(encoding="utf-8-sig"))
                    a = get_anonymizer(task.chat)
                    task.results = _prepare_results(
                        data.get("results") or [], a)
                    if task.fatal is None:
                        task.fatal, task.fatal_code = _prepare_fatal(data, a)
                    note = "результат переанонимизирован из архива"
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("deliver: архив %s не прочитан: %s",
                                   loaded.name, exc)
                    note = "результат утерян (архив не прочитан)"
            elif not _archive_raw_on():
                note = "результат утерян (ARCHIVE_RAW=off)"
            else:
                note = "результат утерян (нет в памяти и в архиве)"
        if task.status == "ready":
            task.status = "delivered"
            task.transitions.setdefault("delivered", _now_iso())
        out = self._view(task)
        if note:
            out["_note"] = note
        return out

    def _restore(self) -> None:
        """Восстановление при старте: файлы обмена — источник истины.
        queue/req_*.json → queued, *.taken → in_1c; results/*.json —
        как обычный тик; *.done/.broken не восстанавливаются — заявка
        потреблена формой; archive/*.loaded в реестр НЕ восстанавливается
        (подгрузка по wait-запросу/revive, compass-5pw)."""
        queue = self.root / "queue"
        if queue.is_dir():
            for p in sorted(queue.glob("req_*.taken")):
                self._restore_req(p, "in_1c")
            for p in sorted(queue.glob("req_*.json")):
                self._restore_req(p, "queued")
        self._tick()

    def _restore_req(self, path: Path, status: str) -> None:
        """Одна задача из req-файла (chat/calls/description — из файла)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("restore: req %s не прочитан: %s", path.name, exc)
            return
        tid = path.name[len("req_"):].rsplit(".", 1)[0]
        if tid in self._tasks:
            return
        calls = data.get("calls")
        if not isinstance(calls, list):
            calls = []
        chat = str(data.get("chat") or "panel")
        created_at = str(data.get("created_at") or _now_iso())
        self._tasks[tid] = Task(
            id=tid, chat=chat,
            description=str(data.get("description") or ""),
            calls=calls, status=status, created_at=created_at,
            transitions={status: _now_iso()})

    def _tick(self) -> None:
        """Один проход watcher'а: .taken → in_1c; финализированные req
        (.json.done/.json.broken) → archive; results → ready/архив;
        поздний ответ из archive/ воскрешает expired; expired; сброс
        results по TTL памяти."""
        now_ts = time.time()
        with self._lock:
            # (а) req_*.json без .taken — ничего не делаем: 1С забирает
            #     сама (бронь переименованием); автоперевода в in_1c нет
            # (г) .taken старше 10 мин без resp — не трогаем (возврат в
            #     очередь — зона формы 1С, arch §11); телеметрии в v1 нет
            for p in (self.root / "queue").glob("req_*.taken"):
                tid = p.name[len("req_"):-len(".taken")]
                task = self._tasks.get(tid)
                if task is not None and task.status == "queued":
                    task.status = "in_1c"
                    task.transitions["in_1c"] = _now_iso()
            # (д) терминальные состояния заявок (compass-5pw): форма
            #     финализирует на месте (.json.done после записи resp,
            #     .json.broken — битый пакет, arch §11); уборка — на
            #     watcher'е: очередь остаётся только с живыми заявками
            for pattern in ("req_*.done", "req_*.broken"):
                for p in (self.root / "queue").glob(pattern):
                    self._retire_queue_file(p)
            for p in (self.root / "results").glob("resp_*.json"):
                self._load_resp(p)
            # (е) результаты важнее TTL (compass-5pw): ответ из archive/
            #     воскрешает expired до свипа _expire
            for task in self._tasks.values():
                if task.status == "expired":
                    self._revive_from_archive(task)
            self._expire(now_ts)
            self._drop_stale_results(now_ts)

    def _load_resp(self, path: Path) -> None:
        """Один resp: матчинг по task_id из имени файла; ready/error;
        архивирование (os.replace — переживает повтор после сбоя).
        Невалидный resp (не JSON-объект, битая кодировка) — карантин
        в archive/*.broken: форма пишет атомарно (arch §11), такой
        файл сам не исцелится, а тик должен жить."""
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError:
            return  # занят при rename — ждёт следующий тик
        except ValueError as exc:
            # JSONDecodeError и UnicodeDecodeError (cp1251 и т.п.)
            self._quarantine_resp(path, f"не читается как JSON: {exc}")
            return
        if not isinstance(data, dict):
            # массив/строка/число верхнего уровня: data.get уронил бы тик
            self._quarantine_resp(path, "JSON верхнего уровня — не объект")
            return
        tid = path.name[len("resp_"):-len(".json")]
        base = data.get("base")
        with self._lock:
            if isinstance(base, dict) and base.get("version"):
                # бейдж версии рантайма (compass-5vq) — из любого resp,
                # включая осиротевший
                self._last_base = {"name": str(base.get("name") or ""),
                                   "version": str(base.get("version"))}
            task = self._tasks.get(tid)
            if task is None:
                # осиротевший resp: не удалять молча — секция
                # «нераспознанные» в панели
                self._archive_resp(path)
                panel_a = get_anonymizer("panel")
                fatal, fatal_code = _prepare_fatal(data, panel_a)
                self._tasks[tid] = Task(
                    id=tid, chat="panel", unrecognized=True,
                    description="нераспознанный ответ (задача не найдена)",
                    calls=[], status="ready",
                    created_at=str(data.get("created_at") or _now_iso()),
                    transitions={"ready": _now_iso()},
                    results=_prepare_results(data.get("results") or [],
                                             panel_a),
                    fatal=fatal, fatal_code=fatal_code)
                return
            if task.status in ("ready", "delivered", "error", "cancelled"):
                # повтор тика после упавшего rename / финальные статусы:
                # данные не трогаем, файл добиваем в архив
                self._archive_resp(path)
                return
            # queued / in_1c / expired — полный путь (поздний resp
            # воскрешает: данные всё ещё ценны, arch §9)
            a = get_anonymizer(task.chat)
            task.results = _prepare_results(data.get("results") or [], a)
            task.fatal, task.fatal_code = _prepare_fatal(data, a)
            task.status = "error" if task.fatal else "ready"
            task.transitions[task.status] = _now_iso()
            self._archive_resp(path)

    def _quarantine_resp(self, path: Path, reason: str) -> None:
        """Карантин невалидного resp: archive/resp_<id>.broken — по образцу
        .broken заявок (arch §11); там файл досматривает TTL-чистка.
        Упало — повтор на следующем тике."""
        logger.warning("resp %s → карантин: %s", path.name, reason)
        try:
            path.replace(self.root / "archive" / (path.stem + ".broken"))
        except OSError as exc:
            logger.warning("resp %s не перенесён в карантин: %s",
                           path.name, exc)

    def _archive_resp(self, path: Path) -> None:
        """Утилизация загруженного resp: по умолчанию → archive/resp_X.loaded
        (долговечная копия сырья — переанонимизация и owner-деанон); при
        ARCHIVE_RAW=off файл удаляется сразу после анонимизации в память.
        Упало — повтор на следующем тике."""
        if not _archive_raw_on():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("resp %s не удалён (ARCHIVE_RAW=off): %s",
                               path.name, exc)
            return
        target = self.root / "archive" / (path.stem + ".loaded")
        try:
            path.replace(target)
        except OSError as exc:
            logger.warning("resp %s не перенесён в архив: %s",
                           path.name, exc)

    def _retire_queue_file(self, path: Path) -> None:
        """Финализированная формой 1С заявка (.json.done / .json.broken):
        queue/ → archive/ под тем же именем (протокол arch §4); там её
        досматривает TTL-чистка _cleanup. Само переименование делает
        форма (arch §11, на клиенте); уборка из очереди — здесь.
        Упало — повтор на следующем тике."""
        try:
            path.replace(self.root / "archive" / path.name)
        except OSError as exc:
            logger.warning("req %s не перенесён в архив: %s", path.name, exc)

    def _revive_from_archive(self, task: Task) -> bool:
        """Поздний ответ переживает TTL (compass-5pw): если ответ уже в
        archive/resp_<id>.loaded (форма успела выполнить обмен, а задача
        ушла в expired — свип TTL или рестарт), задача воскрешается:
        результаты переанонимизируются словарём чата, статус ready/error.
        False — ответа нет: expired остаётся честным отказом."""
        if task.status != "expired":
            return False
        loaded = self.root / "archive" / f"resp_{task.id}.loaded"
        if not loaded.exists():
            return False
        try:
            data = json.loads(loaded.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("revive: архив %s не прочитан: %s",
                           loaded.name, exc)
            return False
        a = get_anonymizer(task.chat)
        task.results = _prepare_results(data.get("results") or [], a)
        task.fatal, task.fatal_code = _prepare_fatal(data, a)
        task.status = "error" if task.fatal else "ready"
        task.transitions[task.status] = _now_iso()
        return True

    def _expire(self, now_ts: float) -> None:
        """Задачи queued/in_1c старше _EXPIRE_SEC → expired. Свип идёт
        ПОСЛЕ подхвата results/archive в том же тике: доставленный
        ответ (вовремя или поздно) всегда успевает перевести задачу в
        ready/error — TTL не убивает задачу с пришедшим ответом
        (compass-5pw)."""
        for task in self._tasks.values():
            if task.status in ("queued", "in_1c"):
                created = _iso_ts(task.created_at)
                if created and now_ts - created > _EXPIRE_SEC:
                    task.status = "expired"
                    task.transitions["expired"] = _now_iso()

    def _drop_stale_results(self, now_ts: float) -> None:
        """Память: results живёт _RESULT_TTL от перехода в ready; истёк —
        None (долговечная копия — archive/*.loaded)."""
        for task in self._tasks.values():
            if task.results is None or task.status not in ("ready", "delivered"):
                continue
            ready_ts = _iso_ts(task.transitions.get("ready"))
            if ready_ts and now_ts - ready_ts > _RESULT_TTL:
                task.results = None

    def _cleanup(self) -> None:
        """Раз в час: archive/ по TTL (_ARCHIVE_TTL) + память (на случай,
        если watcher спал между тиками)."""
        archive = self.root / "archive"
        now_ts = time.time()
        if archive.is_dir():
            for p in archive.iterdir():
                try:
                    if now_ts - p.stat().st_mtime > _ARCHIVE_TTL:
                        p.unlink()
                except OSError as exc:
                    logger.warning("cleanup: %s не удалён: %s", p.name, exc)
        with self._lock:
            self._drop_stale_results(now_ts)

    def start(self) -> "TaskRegistry":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="offline-watcher")
            self._thread.start()
        return self

    def _run(self) -> None:
        last_cleanup = 0.0
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # watcher не умирает от разовой ошибки, но не молчит
                logger.exception("watcher: сбой тика (продолжаем)")
            if time.time() - last_cleanup > 3600:
                try:
                    self._cleanup()
                except Exception:
                    logger.exception("watcher: сбой cleanup (продолжаем)")
                last_cleanup = time.time()
            self._stop.wait(_WATCH_INTERVAL)


_registry: TaskRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> TaskRegistry:
    """Ленивый синглтон (watcher стартует при первом обращении)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = TaskRegistry().start()
        return _registry


def offline_summary() -> dict:
    """Сводка конвейера для /api/status: счётчики статусов + последняя задача.

    Реестр должен быть уже создан (main стартует при RUNTIME_MODE=offline);
    если нет — честный пустой словарь без создания (лениво не тянем файлы
    в горячие статусы, P43-примечание)."""
    with _registry_lock:
        reg = _registry
    if reg is None:
        return {"mode": "offline", "counts": {}, "last": None}
    counts: dict[str, int] = {}
    last = None
    for t in reg.list():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        if not t.get("unrecognized"):
            if last is None or t.get("created_at", "") > last.get("created_at", ""):
                last = {"id": t["id"], "description": t.get("description", ""),
                        "status": t["status"], "created_at": t.get("created_at")}
    return {"mode": "offline", "counts": counts, "last": last}


def reset_registry_for_tests(root: Path) -> TaskRegistry:
    """Тестовый хелпер: новый реестр на tmp-каталоге, watcher НЕ запущен."""
    global _registry
    with _registry_lock:
        _registry = TaskRegistry(root)
        return _registry
