# -*- coding: utf-8 -*-
"""Тесты журнала вызовов: запись, хвост, ротация с ограничением хранений."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.journal import Journal  # noqa: E402


def test_call_write_and_tail(tmp_path):
    j = Journal(directory=tmp_path, max_bytes=10**9, keep=5)
    j.call("search_function", 12.3, True, {"query": "тест"}, result_size=100)
    j.call("read_function", 5.0, False)
    rows = j.tail()
    assert len(rows) == 2
    assert rows[0]["tool"] == "read_function"  # новые сверху
    assert rows[1]["ok"] is True and rows[1]["result_chars"] == 100
    assert "ts" in rows[0]


def test_args_clipped_and_private_hidden(tmp_path):
    j = Journal(directory=tmp_path)
    j.call("t", 1.0, True, args={"q": "x" * 1000, "_secret": "hidden"})
    (line,) = tmp_path.joinpath("calls.jsonl").read_text(
        encoding="utf-8").splitlines()
    rec = json.loads(line)
    assert "_secret" not in rec["args"]
    assert len(rec["args"]["q"]) < 500  # обрезано


def test_rotation_keeps_limit(tmp_path):
    j = Journal(directory=tmp_path, max_bytes=200, keep=2)
    for i in range(30):  # каждая запись ~60 байт — ротаций будет несколько
        j.call(f"tool{i}", 1.0, True)
    rotated = list(tmp_path.glob("calls-*.jsonl"))
    assert 1 <= len(rotated) <= 2  # keep=2
    assert tmp_path.joinpath("calls.jsonl").exists()


def test_stats_24h(tmp_path):
    j = Journal(directory=tmp_path)
    j.call("a", 1.0, True)
    j.call("b", 1.0, False)
    st = j.stats_24h()
    assert st["calls_24h"] == 2
    assert st["errors_24h"] == 1
    assert len(st["hourly"]) == 24
    # оба вызова — в текущий час (последний слот)
    assert st["hourly"][-1]["calls"] == 2
    assert st["hourly"][-1]["errors"] == 1


def test_hourly_buckets_and_counts(tmp_path, monkeypatch):
    """Записи группируются по часам; calls/errors сходятся; старое — мимо.

    now фиксируется подменой datetime в модуле journal: тест зависел от
    минуты часа — в первые 3 минуты now-1h-3min уезжал в соседний слот
    и h1 недосчитывался (флейк, поймано прогоном в 13:01 UTC).
    """
    from datetime import datetime, timedelta, timezone

    from src import journal as journal_mod

    fixed = datetime(2026, 6, 11, 14, 23, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz is not None else fixed

    monkeypatch.setattr(journal_mod, "datetime", _FixedDatetime)
    j = Journal(directory=tmp_path)
    now = fixed
    rows = [
        {"ts": (now - timedelta(hours=1)).isoformat(), "tool": "a", "ok": True},
        {"ts": (now - timedelta(hours=1, minutes=3)).isoformat(),
         "tool": "b", "ok": False},
        {"ts": (now - timedelta(hours=2)).isoformat(), "tool": "c", "ok": True},
        # старше 24 ч — не считается ни в итогах, ни в гистограмме
        {"ts": (now - timedelta(hours=30)).isoformat(), "tool": "old", "ok": True},
    ]
    j.path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stats = j.stats_24h()
    assert stats["calls_24h"] == 3
    assert stats["errors_24h"] == 1
    hourly = stats["hourly"]
    assert len(hourly) == 24
    # старейший слот — час now-23h, первым в списке
    assert hourly[0]["hour"] == (now - timedelta(hours=23)).strftime("%d.%m %H:00")
    by_hour = {h["hour"]: h for h in hourly}
    h1 = by_hour[(now - timedelta(hours=1)).strftime("%d.%m %H:00")]
    assert h1["calls"] == 2 and h1["errors"] == 1
    h2 = by_hour[(now - timedelta(hours=2)).strftime("%d.%m %H:00")]
    assert h2["calls"] == 1 and h2["errors"] == 0


def test_hourly_empty_log_all_zeroes(tmp_path):
    """Пустой журнал: 24 нулевых слота."""
    j = Journal(directory=tmp_path)
    stats = j.stats_24h()
    assert stats["calls_24h"] == 0
    assert len(stats["hourly"]) == 24
    assert all(h["calls"] == 0 and h["errors"] == 0 for h in stats["hourly"])


def test_hourly_skips_broken_ts(tmp_path):
    """Мусорный ts пропускается, не ломая проход."""
    from datetime import datetime, timezone

    j = Journal(directory=tmp_path)
    now = datetime.now(timezone.utc)
    j.path.write_text("\n".join([
        json.dumps({"ts": "не-дата", "tool": "x", "ok": True}),
        json.dumps({"ts": now.isoformat(), "tool": "ok", "ok": True}),
    ]) + "\n", encoding="utf-8")
    stats = j.stats_24h()
    assert stats["calls_24h"] == 1
    assert stats["hourly"][-1]["calls"] == 1
