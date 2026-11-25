# -*- coding: utf-8 -*-
"""Двусторонний обезличиватель данных (токенизация).

Задача (#32, офлайн-режим): данные из базы 1С приходят в ядро — прежде
чем отдать их агенту (облако LLM), маскируются коммерчески значимые
строки. Агент работает с токенами; когда он пишет код, оперируя
токенами, ядро подставляет реальные значения обратно.

White-list (НЕ маскируется): имена метаданных, перечисления, числа,
даты, булевы. Маскируются: строки-представления (наименования
контрагентов, ФИО, суммы строковых полей).

Словарь соответствий — in-memory, свой на каждый чат агента (ключ —
X-Chat-Name): токены чата A не деанонимизируются в чате B. TTL 24 ч от
последнего доступа, лимит 10k токенов на чат (переполнение — честный
сброс); persistent-опция — при подтверждении безопасности на проде.
"""
from __future__ import annotations

import re
import threading
from typing import Any

# Типы полей, которые маскируются (по имени поля)
_SENSITIVE_FIELD_PATTERNS = re.compile(
    r"(наименование|контрагент|клиент|организация|комментарий|адрес|"
    r"почта|телефон|инн|кпп|фио|фамилия|имя|отчество|"
    r"документ|номер)",
    re.IGNORECASE,
)

# Whitelist сквозной маскировки ответа (compass-lz7): ISO-даты и ГУИДы
# проходят как есть — это ключи/периоды, не коммерческие строки. Числа,
# булевы и None — не строки и так проходят; пустые отсекает
# anonymize_value. Дата — fullmatch с опциональным временем (T/пробел,
# секунды/доли, таймзона) — «2026-11-17» и «2026-11-17T10:00:00+03:00».
_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class TokenAnonymizer:
    """Двусторонний словарь токен ↔ исходное значение.

    Токены — валидные BSL-идентификаторы (Контрагент_Т1), чтобы код
    агента компилировался после прямой подстановки.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._forward: dict[str, str] = {}   # значение → токен
        self._backward: dict[str, str] = {}  # токен → значение
        self._counters: dict[str, int] = {}  # префикс → счётчик

    def anonymize_value(self, value: str, prefix: str = "Значение") -> str:
        """Заменить строку токеном (создать или вернуть существующий)."""
        if not isinstance(value, str) or not value.strip():
            return value
        with self._lock:
            if value in self._forward:
                return self._forward[value]
            n = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = n
            token = f"{prefix}_Т{n}"
            self._forward[value] = token
            self._backward[token] = value
            return token

    def deanonymize_value(self, token: str) -> str | None:
        """Токен → исходное значение (None, если токен неизвестен)."""
        with self._lock:
            return self._backward.get(token)

    def _should_mask(self, key: str, value: Any) -> bool:
        """Решение: маскировать ли это значение по ключу (имени поля)."""
        if not isinstance(value, str) or len(value) < 2:
            return False
        # Представления ссылок («Справочник.Контрагенты.Наименование»)
        if "Справочник" in value or "Документ" in value:
            return True
        # Поля с чувствительными именами
        if _SENSITIVE_FIELD_PATTERNS.search(key):
            return True
        return False

    def _prefix_for(self, key: str) -> str:
        """Префикс токена из имени поля («НаименованиеКонтрагента» → «Контрагент»)."""
        key_lower = key.lower()
        for keyword, prefix in (
            ("контрагент", "Контрагент"),
            ("организация", "Организация"),
            ("клиент", "Клиент"),
            ("номенклатура", "Номенклатура"),
            ("наименование", "Значение"),
            ("комментарий", "Комментарий"),
            ("адрес", "Адрес"),
        ):
            if keyword in key_lower:
                return prefix
        return "Значение"

    def anonymize_data(self, data: Any, depth: int = 0) -> Any:
        """Рекурсивно пройтись по данным, маскируя чувствительные строки.

        dict → маскирует значения по ключам; list → рекурсивно;
        строки сами по себе НЕ маскируются (только в контексте полей).
        """
        if depth > 8:
            return data
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                if self._should_mask(k, v):
                    out[k] = self.anonymize_value(v, self._prefix_for(k))
                else:
                    out[k] = self.anonymize_data(v, depth + 1)
            return out
        if isinstance(data, list):
            return [self.anonymize_data(item, depth + 1) for item in data]
        return data

    def anonymize_response(self, data: Any, key: str = "",
                           depth: int = 0) -> Any:
        """Сквозная маскировка ответа 1С (compass-lz7, P0): ВСЕ строковые
        значения → per-chat токен, кроме whitelist (_ISO_DATE_RE, _GUID_RE,
        пустые; числа/булевы — не строки). Ключи структур и имена колонок
        не маскируются — только значения ячеек; префикс токена осмысленный
        от имени поля (_prefix_for: Контрагент_Т3), прочее — Значение_ТN.
        Деанонимизация новых токенов работает автоматически (общий словарь).
        """
        if depth > 8:
            return data
        if isinstance(data, dict):
            return {k: self.anonymize_response(v, key=str(k), depth=depth + 1)
                    for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.anonymize_response(item, key=key, depth=depth + 1)
                    for item in data]
        if isinstance(data, str):
            if (not data.strip() or _ISO_DATE_RE.match(data)
                    or _GUID_RE.match(data)):
                return data
            return self.anonymize_value(data, self._prefix_for(key))
        return data

    def deanonymize_text(self, text: str) -> str:
        """Заменить все токены в тексте на исходные значения.

        Для кода агента: «Контрагент_Т1.Наименование» → «ООО Ромашка.Наименование»
        """
        result = text
        with self._lock:
            for token, value in sorted(
                self._backward.items(), key=lambda kv: -len(kv[0])
            ):
                result = result.replace(token, value)
        return result

    def deanonymize_data(self, data: Any) -> Any:
        """Рекурсивный деанонимизатор для структур данных."""
        if isinstance(data, dict):
            return {k: self.deanonymize_data(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.deanonymize_data(item) for item in data]
        if isinstance(data, str):
            return self.deanonymize_text(data)
        return data

    def stats(self) -> dict:
        """Сводка словаря для панели."""
        with self._lock:
            return {"tokens": len(self._forward),
                    "prefixes": dict(self._counters)}

    def clear(self) -> None:
        """Сброс словаря (новая сессия обезличивания)."""
        with self._lock:
            self._forward.clear()
            self._backward.clear()
            self._counters.clear()


# ------------------------------------------------- per-chat словари (Т2 §2)
#
# Один TokenAnonymizer на чат агента (ключ — X-Chat-Name): токены чата A
# не деанонимизируются в чате B. TTL 24 ч от последнего доступа (лениво),
# лимит 10k токенов на чат — при переполнении честный сброс со счётчиком.

_CHAT_TTL_SEC = 24 * 3600
_CHAT_TOKEN_LIMIT = 10_000

_chats: dict[str, dict] = {}   # chat -> {"a": TokenAnonymizer, "touched": float, "resets": int}
_lock = threading.Lock()


def _now() -> float:
    import time
    return time.time()


def _purge_expired(now: float) -> None:
    """Ленивая TTL-чистка (под локом): неактивные чаты 24 ч — долой."""
    stale = [c for c, rec in _chats.items()
             if now - rec["touched"] > _CHAT_TTL_SEC]
    for c in stale:
        _chats.pop(c, None)


def get_anonymizer(chat: str | None = None) -> TokenAnonymizer:
    """Словарь токенов чата. chat=None → текущий контекст (X-Chat-Name,
    дефолт "panel"). Создаёт/продлевает TTL; при переполнении — сброс."""
    if chat is None:
        from ..context import current_context_name
        chat = current_context_name() or "panel"
    chat = (chat or "panel").strip()[:100] or "panel"
    now = _now()
    with _lock:
        _purge_expired(now)
        rec = _chats.get(chat)
        if rec is None:
            rec = {"a": TokenAnonymizer(), "touched": now, "resets": 0}
            _chats[chat] = rec
        rec["touched"] = now
        if len(rec["a"]._forward) > _CHAT_TOKEN_LIMIT:
            rec["a"].clear()
            rec["resets"] += 1
        return rec["a"]


def chat_resets(chat: str | None = None) -> int:
    """Сколько раз словарь чата сбрасывался (для честной пометки в ответах)."""
    if chat is None:
        from ..context import current_context_name
        chat = current_context_name() or "panel"
    with _lock:
        return (_chats.get(chat) or {}).get("resets", 0)


def all_stats() -> dict:
    """Сводка по всем чатам (панель): токены, resets, возраст."""
    now = _now()
    with _lock:
        _purge_expired(now)
        return {c: {"tokens": len(rec["a"]._forward),
                    "resets": rec["resets"],
                    "idle_sec": int(now - rec["touched"])}
                for c, rec in sorted(_chats.items())}
