# -*- coding: utf-8 -*-
"""Тулы рантайм-контура (Р1, разведка данными — поворот владельца #32).

Обработка в сеансе 1С отдаёт живые данные базы (read-only): запросы,
объекты по ключу, записи регистров, примеры справочников. Песочница
выполнения кода отложена — sandbox_run не регистрируется.

compass-vlu: отказы рантайма ведут к выходу — подсказка называет ОБА пути
(RUNTIME_URL по HTTP / RUNTIME_MODE=offline через файловый обмен) и как
их включить (env контейнера, рестарт ядра); ложное «онлайн-тулы работают
напрямую» при незаданном RUNTIME_URL убрано.
"""
from __future__ import annotations

from ..runtime.client import RuntimeUnavailable, runtime_client
from ..runtime import offline
# compass-c6z (§7 №1-2): бриф читает только env — живёт в ядре ядра,
# слой рантайма (S3) здесь больше не поднимается eager-импортом.
# Реэкспорт под прежним именем: main.py (/api/status) зовёт
# runtime_tools._runtime_brief() через атрибут модуля — контракт сохранён
from ..runtime_mode import runtime_brief as _runtime_brief  # noqa: F401

# Честные пути выхода из промежуточной конфигурации «online + URL не задан»:
# обе настройки — env контейнера, применяются рестартом ядра (vlu)
_RUNTIME_EXIT_HINT = (
    "выхода два (env контейнера, применяются рестартом ядра): "
    "(1) попросите владельца задать RUNTIME_URL — в базе 1С должна "
    "работать обработка CompassRuntime (обмен по HTTP; при настройке — "
    "токен RUNTIME_TOKEN); "
    "(2) задать RUNTIME_MODE=offline — тогда заработает офлайн-конвейер: "
    "offline_submit соберёт пакет, 1С заберёт его формой «Обмен», "
    "ответ — offline_wait")

# RLS-факты из боя (feedback-2026-11-17, compass-lz7): класс кода
# ACCESS_DENIED чаще всего означает ограничение на уровне записей, а не
# ошибку агента — те же данные обычно читаются через поля документов.
_RLS_HINT = (
    "код ACCESS_DENIED — обычно RLS (ограничение прав на уровне записей), "
    "а не ошибка агента: прямые выборки по справочникам (Организации, "
    "Контрагенты, Склады) падают с «нет прав», хотя те же данные читаются "
    "разыменованиями в полях документов (Товары.Склад, Организация)")


def _offline_mode() -> bool:
    """Офлайн-режим: файловый обмен вместо HTTP (RUNTIME_MODE=offline)."""
    import os
    return os.getenv("RUNTIME_MODE", "online").lower() == "offline"


def _runtime_error(exc: RuntimeUnavailable) -> dict:
    """Отказ рантайм-вызова с путями выхода (compass-vlu).

    «RUNTIME_URL не задан» — штатная промежуточная конфигурация, но обе
    дорожки до живой базы тогда перекрыты; подсказка ведёт к любому из
    двух. Прочие отказы (недоступен, таймаут) отдаются как есть — там
    контур настроен, путь выхода другой."""
    err = str(exc)
    if "не задан" in err or "не подключён" in err:
        return {"error": err, "hint": _RUNTIME_EXIT_HINT}
    return {"error": err}


def runtime_status() -> dict:
    """Состояние рантайм-контура: режим, подключение, офлайн-обмен.
    В офлайне — подсказка про RLS (компас-lz7): ACCESS_DENIED в ошибках
    пакета почти всегда означает ограничение записей, не ошибку агента.

    compass-82a: во все ветки добавлен блок update — актуальность версии
    контура vs GitHub main (из кэша, без сети): агент при входе в контур
    сразу видит, отстаёт ли образ."""
    from .. import update_check
    if _offline_mode():
        out = {"mode": "offline", "rls_hint": _RLS_HINT,
               **offline.offline_status()}
    else:
        client = runtime_client()
        if not client.configured:
            out = {"mode": "online", "configured": False,
                   "hint": "онлайн-тулы не подключены. " + _RUNTIME_EXIT_HINT}
        else:
            try:
                pong = client.ping()
                out = {"mode": "online", "configured": True, **pong}
            except RuntimeUnavailable as exc:
                out = {"mode": "online", "configured": True,
                       "reachable": False, "error": str(exc)}
    out["update"] = update_check.update_status()
    return out


def _offline_guard(method: str, params: dict | None = None) -> dict | None:
    """Офлайн-режим: единая подсказка на пакетный конвейер (arch §2)."""
    if not _offline_mode():
        return None
    return {"info": "офлайн-режим: одиночный вызов не формируется — "
                    "соберите пакет через offline_submit([...]) и дождитесь "
                    "offline_wait(task_id)",
            "method": method}


def data_query(query: str, limit: int = 10,
               params: dict | None = None) -> dict:
    """Разведка данных: SELECT к живой базе (только чтение). Параметры
    запроса — &Имя: значение.

    Контракт лимита (compass-xuy): обработка 1С принудительно ограничивает
    выборку — если в тексте запроса нет своего ПЕРВЫЕ/TOP, она вставляет
    «ПЕРВЫЕ N+1» в текст запроса (защита от неограниченных выборок; N =
    limit, по умолчанию 10 → «ПЕРВЫЕ 11»; строка N+1 нужна обработке
    только для детекции продолжения ЕщеЕсть и в ответ не попадает).
    Запрос со своим ПЕРВЫЕ/TOP исполняется как написан (усечение по
    limit после выполнения). Фактический лимит виден по числу строк
    ответа; эхо запроса в ошибках — исполненный текст с инжекцией.

    Курсор (compass-5vq): при ЕщеЕсть=true в ответе Курсор=null и в
    офлайне, и в онлайне — keyset-продолжение data_query не строится
    в принципе (обработка 1С строит курсор только для records, где
    известен ключ уникальности). Продолжайте выборку через records
    или сужайте запрос (отборы/ПЕРВЫЕ N+смещение через ГДЕ).

    Пример: data_query("ВЫБРАТЬ Ссылка, Наименование ИЗ Справочник.Контрагенты")
    """
    guard = _offline_guard("data_query",
                           {"query": query, "limit": limit, "params": params or {}})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.data_query(query, params=params, limit=limit)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def get_object(type_name: str, name: str, key: str,
               key_kind: str = "код") -> dict:
    """Объект базы по ключу: реквизиты, табличные части (первые строки),
    пометка проведения. key_kind: код | наименование | гуид.

    Пример: get_object("Справочник", "Контрагенты", "000000001")"""
    guard = _offline_guard("get_object",
                           {"type": type_name, "name": name, "key": key,
                            "key_kind": key_kind})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.get_object(type_name, name, key, key_kind)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def register_records(register_name: str, limit: int = 10) -> dict:
    """Записи регистра (проводки/остатки/сведения) — срез первых N строк.

    Пример: register_records("Продажи")"""
    guard = _offline_guard("register_records",
                           {"register": register_name, "limit": limit})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.register_records(register_name, limit)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def catalog_samples(catalog_name: str, limit: int = 5) -> dict:
    """Как заполняются элементы справочника: первые N элементов со всеми
    реквизитами — паттерны наполнения живой базы.

    Пример: catalog_samples("Номенклатура", 5)"""
    guard = _offline_guard("catalog_samples",
                           {"catalog": catalog_name, "limit": limit})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.catalog_samples(catalog_name, limit)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def records(Объект: str, Поля=None, Отборы=None, Порядок=None,
            Лимит: int = 100, Курсор: str = "", Виртуальная: str = "",
            ПараметрыВТ=None) -> dict:
    """Универсальная выборка записей объекта — один тул для всех видов
    метаданных (справочники, документы, регистры, планы, задачи,
    перечисления), с автоматическим keyset-курсором (arch §2).

    Параметры:
        Объект - «Вид.Имя» как в языке запросов, напр.
                 «РегистрНакопления.Продажи» (вид обязателен, передаётся
                 как есть — разбор на стороне обработки);
        Поля - список имён полей; пустой/не задан = все (*);
        Отборы - список структур {Поле, Операция, Значение}; операции:
                 eq, ne, gt, ge, lt, le, contains, inList, isNull,
                 isNotNull; соединение только И; Значение при inList —
                 список; даты — строка ГГГГММДДЧЧММСС;
        Порядок - список структур {Поле, Направление: "возр"|"убыв"};
                  ключ уникальности для курсора добавляется автоматически;
        Лимит - потолок 1000, дефолт 100;
        Курсор - opaque-продолжение прошлого ответа (Курсор из ответа);
        Виртуальная - "Остатки"|"Обороты"|"СрезПоследних"|"" (регистры);
        ПараметрыВТ - соответствие {Период|НачалоПериода|КонецПериода:
                      строка-дата ГГГГММДДЧЧММСС}.

    Пример: records("РегистрНакопления.Продажи",
                    Поля=["Период", "Регистратор", "Количество"],
                    Отборы=[{"Поле": "Период", "Операция": "ge",
                             "Значение": "20260101000000"}],
                    Порядок=[{"Поле": "Период", "Направление": "убыв"}],
                    Лимит=200)
    """
    guard = _offline_guard("records",
                           {"object": Объект, "fields": Поля or [],
                            "filters": Отборы or [], "order": Порядок or [],
                            "limit": Лимит, "cursor": Курсор,
                            "virtual": Виртуальная,
                            "vt_params": ПараметрыВТ or {}})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.records(Объект, fields=Поля, filters=Отборы,
                              order=Порядок, limit=Лимит, cursor=Курсор,
                              virtual=Виртуальная, vt_params=ПараметрыВТ)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def document_movements(ТипОбъекта: str, Имя: str, Ключ: str,
                       ВидКлюча: str = "код",
                       ЛимитНаРегистр: int = 10) -> dict:
    """Движения одного документа по всем регистрам: обзор количества
    записей по регистрам (только непустые) + строки движений.

    Параметры:
        ТипОбъекта, Имя, Ключ, ВидКлюча - как в get_object
        (ВидКлюча: код | наименование | гуид);
        ЛимитНаРегистр - потолок строк на регистр (макс 100).

    Пример: document_movements("Документ", "РеализацияТоваровУслуг",
                               "00-000001")
    """
    guard = _offline_guard("document_movements",
                           {"type": ТипОбъекта, "name": Имя, "key": Ключ,
                            "key_kind": ВидКлюча,
                            "limit_per_register": ЛимитНаРегистр})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.document_movements(ТипОбъекта, Имя, Ключ,
                                         key_kind=ВидКлюча,
                                         limit_per_register=ЛимитНаРегистр)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def event_log(НачалоПериода: str, КонецПериода: str, Объект: str = "",
              Пользователь: str = "", Событие: str = "",
              Лимит: int = 100) -> dict:
    """Записи журнала регистрации за период (arch §13), сортировка по
    времени убыв. Пагинация — сузить период по дате первой строки.

    Параметры:
        НачалоПериода, КонецПериода - обязательны, окно ≤ 31 дня,
                 строки ГГГГММДДЧЧММСС;
        Объект - строка-ГУИД или "" (фильтр по объекту);
        Пользователь, Событие - строки или "";
        Лимит - потолок 1000, дефолт 100.

    Пример: event_log("20261101000000", "20261112120000", Событие="Данные.Проведение")
    """
    guard = _offline_guard("event_log",
                           {"start": НачалоПериода, "end": КонецПериода,
                            "object": Объект, "user": Пользователь,
                            "event": Событие, "limit": Лимит})
    if guard:
        return guard
    client = runtime_client()
    try:
        return client.event_log(НачалоПериода, КонецПериода, object_guid=Объект,
                                user=Пользователь, event=Событие, limit=Лимит)
    except RuntimeUnavailable as exc:
        return _runtime_error(exc)


def offline_deanonymize(text: str) -> dict:
    """Токены в тексте → исходные значения (для проверки кода агентом).

    Обратная сторона обезличивания (compass-lz7): «Контрагент_Т1» →
    исходное значение из словаря ТЕКУЩЕГО чата (токены чужого чата и
    неизвестные строки остаются как есть)."""
    return {"original": offline.deanonymize(text)}


def offline_submit(calls: list[dict], description: str = "") -> dict:
    """Офлайн: собрать пакет вызовов в задачу (queue/req_<id>.json).

    Агент НЕ пишет файлы сам: шлёт вызовы [{method, params}] — компас
    собирает пакет (токены ссылок подставляются ядром), 1С заберёт через
    форму «Обмен». Затем offline_wait(task_id).

    Обезличивание (compass-lz7): params при отправке деанонимизируются
    словарём чата (токен → исходное значение), 1С получает реальные
    ключи; в ответе все строковые значения маскируются при сериализации —
    токенами per-chat (Контрагент_Т3, Значение_ТN...), КРОМЕ whitelist:
    числа/булевы, ISO-даты (ГГГГ-ММ-ДД[Т чч:мм[:сс]]), GUID (36 симв.),
    пустые строки; ключи колонок и служебные поля (method/ok/code) не
    маскируются. Одинаковые значения → один токен; см. SECURITY.md.

    Пример: offline_submit([{"method": "data_query",
        "params": {"query": "ВЫБРАТЬ ПЕРВЫЕ 10 * ИЗ Справочник.Контрагенты"}}],
        "остатки по контрагенту")"""
    if not _offline_mode():
        import os
        if not os.getenv("RUNTIME_URL", ""):
            # vlu: обе дорожки перекрыты — не «онлайн-тулы работают
            # напрямую», а честный тупик с путями выхода
            return {"error": "обе дорожки до живой базы перекрыты: "
                             "RUNTIME_MODE=online при незаданном "
                             "RUNTIME_URL — онлайн-тулы не подключены, "
                             "офлайн-конвейер выключен",
                    "hint": _RUNTIME_EXIT_HINT}
        return {"info": "офлайн-режим выключен (RUNTIME_MODE=online); "
                        "онлайн-тулы работают через RUNTIME_URL"}
    from ..context import current_context_name
    from ..runtime.tasks import get_registry
    try:
        task = get_registry().submit(current_context_name() or "panel",
                                     calls, description)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"task_id": task.id, "status": task.status,
            "description": task.description,
            "calls": len(task.calls),
            "hint": "откройте форму «Обмен» в 1С (или ждите автополлинг); "
                    "затем offline_wait(task_id)"}


def offline_wait(task_id: str, timeout: int = 120) -> dict:
    """Офлайн: дождаться результата задачи (короткие циклы, ≤ timeout сек).

    Рекомендуется timeout ≤ 120 — длинный вызов может оборвать
    MCP-клиент; повторный вызов идемпотентен.

    В ошибках вызова код приходит в поле code (ACCESS_DENIED |
    FIELD_NOT_FOUND | SYNTAX_ERROR | OTHER, compass-lz7) — класс не
    маскируется; ACCESS_DENIED обычно RLS (см. rls_hint в runtime_status).
    Лёгкий статус без ожидания — offline_task_status(task_id)."""
    if not _offline_mode():
        import os
        if not os.getenv("RUNTIME_URL", ""):
            return {"error": "офлайн-режим выключен (RUNTIME_MODE=online), "
                             "а RUNTIME_URL не задан — результата нет ни по "
                             "одной дорожке",
                    "hint": _RUNTIME_EXIT_HINT}
        return {"info": "офлайн-режим выключен (RUNTIME_MODE=online)"}
    from ..context import current_context_name
    from ..runtime.tasks import get_registry
    return get_registry().wait(current_context_name() or "panel",
                               task_id, timeout=min(max(1, timeout), 600))


def offline_task_status(task_id: str) -> dict:
    """Офлайн: лёгкий статус задачи по id БЕЗ входа в offline_wait
    (compass-5vq): статус, переходы, наличие результата — без блокировки.

    Результаты (токены) не возвращаются — за ними offline_wait(task_id);
    wait идемпотентен и выдаст их повторно. Задача другого чата/неизвестный
    id → честный отказ + живые задачи своего чата."""
    if not _offline_mode():
        import os
        if not os.getenv("RUNTIME_URL", ""):
            return {"error": "офлайн-режим выключен (RUNTIME_MODE=online), "
                             "а RUNTIME_URL не задан — результата нет ни по "
                             "одной дорожке",
                    "hint": _RUNTIME_EXIT_HINT}
        return {"info": "офлайн-режим выключен (RUNTIME_MODE=online)"}
    from ..context import current_context_name
    from ..runtime.tasks import get_registry
    return get_registry().status_view(
        current_context_name() or "panel", task_id)
