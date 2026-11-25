# -*- coding: utf-8 -*-
"""Журнал вызовов тулов (calls.jsonl) с ротацией по размеру.

Наблюдаемость изделия: кто/что/как долго/сколько весил ответ — без docker
logs. Ротация: при превышении max_bytes файл переименовывается в
calls-<UTC-время>.jsonl, хранятся последние keep штук.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MAX_FIELD = 300  # обрезка длинных значений аргументов


class Journal:
    """Потокобезопасный JSONL-журнал с ротацией (один на процесс)."""

    def __init__(self, directory: str | Path | None = None,
                 max_bytes: int = 10 * 1024 * 1024, keep: int = 5):
        self.directory = Path(directory or os.getenv("LOGS_PATH",
                                            "/data/system/logs"))
        self.max_bytes = max_bytes
        self.keep = keep
        self.path = self.directory / "calls.jsonl"
        self._lock = threading.Lock()
        self._enabled = True
        # кэш stats_24h: панель опрашивает /api/status каждые 5 с, а расчёт
        # читает до 10 МБ файла; пересчёт — только когда файл изменился
        # (stat дешевле полного чтения; ключ (mtime_ns, size) надёжен и для
        # ротации — она меняет и то и другое)
        self._stats_key: tuple[int, int] | None = None
        self._stats_value: dict | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._enabled = False

    def _rotate_if_needed(self) -> None:
        """Проверить размер и ротировать (вызывается под локом)."""
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rotated = self.directory / f"calls-{stamp}.jsonl"
            self.path.replace(rotated)
            # держим только последние keep ротаций
            olds = sorted(p for p in self.directory.glob("calls-*.jsonl"))
            for stale in olds[: max(0, len(olds) - self.keep)]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass  # журнал не должен ломать тул ни при каких условиях

    def call(self, tool: str, duration_ms: float, ok: bool,
             args: dict | None = None, result_size: int | None = None) -> None:
        """Записать один вызов тула (при отказе IO — тихо пропустить)."""
        if not self._enabled:
            return
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "ms": round(duration_ms, 1),
            "ok": ok,
        }
        if args:
            record["args"] = {
                k: (v[:_MAX_FIELD] + "…" if isinstance(v, str) and len(v) > _MAX_FIELD
                    else v)
                for k, v in args.items() if not k.startswith("_")
            }
        if result_size is not None:
            record["result_chars"] = result_size
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def tail(self, limit: int = 200) -> list[dict]:
        """Последние записи (для панели) — чтение с конца файла."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        rows: list[dict] = []
        for line in reversed(text.splitlines()):
            if len(rows) >= limit:
                break
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def stats_24h(self) -> dict:
        """Счётчики за сутки + почасовая гистограмма (один проход по хвосту).

        Кэшируется по (mtime_ns, size) файла: между записями в журнал
        повторные вызовы панели отдают готовый результат без чтения.
        hourly — всегда 24 слота (пустые часы — нули), UTC, хронологически:
        [{"hour": "06.11 14:00", "calls": N, "errors": M}, ...] — старейший
        первым. Лимит хвоста 5000 записей: на очень загруженном сервере
        старые часы недосчитываются (ротация 10 МБ тоже режет историю).
        """
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None and key == self._stats_key and \
                self._stats_value is not None:
            return self._stats_value

        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - 86400
        calls = errors = 0
        buckets: dict[str, list[int]] = {}
        for r in self.tail(limit=5000):
            ts = r.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if dt.timestamp() < cutoff:
                continue
            err = 0 if r.get("ok", True) else 1
            calls += 1
            errors += err
            hour_key = dt.strftime("%d.%m %H:00")
            bucket = buckets.setdefault(hour_key, [0, 0])
            bucket[0] += 1
            bucket[1] += err
        hourly = []
        for i in range(23, -1, -1):
            hour_key = (now - timedelta(hours=i)).strftime("%d.%m %H:00")
            c, e = buckets.get(hour_key, (0, 0))
            hourly.append({"hour": hour_key, "calls": c, "errors": e})
        result = {"calls_24h": calls, "errors_24h": errors, "hourly": hourly}
        if key is not None:
            self._stats_key, self._stats_value = key, result
        return result


_journal: Journal | None = None


def get_journal() -> Journal:
    """Ленивый singleton журнала."""
    global _journal
    if _journal is None:
        _journal = Journal()
    return _journal
