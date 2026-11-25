# -*- coding: utf-8 -*-
"""compass-d9t (обратная связь №4): source_grep — regex-поиск по телам
модулей source_root с дедлайном, вне индекса.

compass-sj2: тул асинхронный (обход в asyncio.to_thread, параллель
ограничен семафором 2) — вызовы в тестах заворачиваются в asyncio.run.

Фикстура: tmp-каталог выгрузки с 4 .bsl; паттерн «Записывать(Истина)»
в двух файлах, в одном — три подряд вхождения «Отложить» (слияние
контекстов). Активная конфигурация — через COMPASS_SOURCE_ROOT (env —
легитимный источник config.source_root, как у read_function) и через
реестр для теста config-бейджа.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import workspace as ws  # noqa: E402
from src.facade import tools  # noqa: E402
from src.registry import add_config  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8-sig")


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    """tmp-выгрузка с 4 .bsl; активный source_root через env."""
    root = tmp_path / "src"
    _write(root, "CommonModules/Продажи/Ext/Module.bsl",
           "Процедура Заполнить()\n"
           "\tЗаказ = Документы.Заказ.СоздатьДокумент();\n"
           "\tЗаказ.Записывать(Истина);\n"
           "\tПровестиЗаказ(Заказ);\n"
           "КонецПроцедуры\n")
    _write(root, "CommonModules/Склад/Ext/Module.bsl",
           "Процедура ОтложитьПартию()\n"
           "\tОтложить();\n"
           "\tОтложить();\n"
           "\tОтложить();\n"
           "КонецПроцедуры\n")
    _write(root, "Documents/Реализация/Ext/ObjectModule.bsl",
           "Процедура ПередЗаписью(Отказ)\n"
           "\tОбъект.Записывать(Истина);\n"
           "\t// вариант в нижнем регистре: объект.записывать(истина);\n"
           "КонецПроцедуры\n")
    _write(root, "CommonModules/Пустой/Ext/Module.bsl",
           "Процедура БезВхождений()\n"
           "\tТекстНеВТочномРегистре();\n"
           "КонецПроцедуры\n")
    monkeypatch.setenv("COMPASS_SOURCE_ROOT", str(root))
    monkeypatch.delenv("SOURCE_GREP_TIMEOUT", raising=False)
    yield root


def test_literal_with_escaped_parens(source_tree):
    """Литерал с экранированными скобками/точкой: матчи в нужных файлах,
    file/line_no/context корректны (номера строк в контексте)."""
    out = asyncio.run(tools.source_grep(r"СоздатьДокумент\(\)"))
    assert out["truncated"] is False
    assert [m["file"] for m in out["matches"]] == \
        ["CommonModules/Продажи/Ext/Module.bsl"]
    m = out["matches"][0]
    assert m["line_no"] == 2 and "СоздатьДокумент" in m["line"]
    assert any(n.startswith("1: ") for n in m["context"])
    assert any(n.startswith("3: ") for n in m["context"])


def test_regex_pattern_two_files(source_tree):
    """Regex Записывать\\(Истина\\) → матчи в обоих файлах с паттерном."""
    out = asyncio.run(tools.source_grep(r"Записывать\(Истина\)"))
    files = {m["file"] for m in out["matches"]}
    assert files == {"CommonModules/Продажи/Ext/Module.bsl",
                     "Documents/Реализация/Ext/ObjectModule.bsl"}
    assert out["total_matches_returned"] == 2
    assert out["files_scanned"] == 4


def test_limit_one_truncates(source_tree):
    """limit=1 → один матч и честный truncated=true."""
    out = asyncio.run(tools.source_grep(r"Записывать\(Истина\)", limit=1))
    assert out["total_matches_returned"] == 1
    assert out["truncated"] is True


def test_module_hint_narrows_scan(source_tree):
    """module_hint сужает обход: файлы без подстроки не сканируются."""
    full = asyncio.run(tools.source_grep(r"Отложить"))
    hinted = asyncio.run(tools.source_grep(r"Отложить", module_hint="Склад"))
    assert full["files_total"] == 4 and hinted["files_total"] == 1
    assert full["files_scanned"] == 4 and hinted["files_scanned"] == 1
    assert hinted["matches"][0]["file"].startswith("CommonModules/Склад")


def test_ignorecase_finds_other_case(source_tree):
    """ignorecase=True ловит другую форму регистра («ТЕКСТНЕВТОЧНОМ»
    находит «ТекстНеВТочномРегистре»); без флага — не ловит."""
    pattern = r"ТЕКСТНЕВТОЧНОМ"
    strict = asyncio.run(tools.source_grep(pattern))
    loose = asyncio.run(tools.source_grep(pattern, ignorecase=True))
    assert strict["matches"] == []
    assert loose["total_matches_returned"] == 1
    assert loose["matches"][0]["file"].startswith("CommonModules/Пустой")


def test_bad_regex_and_empty_pattern(source_tree):
    """Битый regex и пустой pattern → error, не исключение."""
    bad = asyncio.run(tools.source_grep("("))
    assert "error" in bad and "regex" in bad["error"]
    assert "error" in asyncio.run(tools.source_grep("   "))
    assert "error" in asyncio.run(tools.source_grep("x" * 201))


def test_adjacent_hits_merged(source_tree):
    """Три подряд вхождения «Отложить» → ≤2 записей (слияние контекстов)."""
    out = asyncio.run(tools.source_grep(r"Отложить\(\);"))
    hits = [m for m in out["matches"]
            if m["file"].startswith("CommonModules/Склад")]
    assert len(hits) <= 2


def test_deadline_zero_returns_truncated(source_tree, monkeypatch):
    """Дедлайн ~0 → ответ вернулся (не завис), truncated=true, скан пуст."""
    monkeypatch.setattr(tools, "_SOURCE_GREP_TIMEOUT", 0.0)
    out = asyncio.run(tools.source_grep(r"Отложить"))
    assert out["truncated"] is True
    assert out["files_scanned"] == 0
    assert out["matches"] == []


def test_five_concurrent_within_deadline(source_tree):
    """sj2: 5 конкурентных source_grep на малом source_root завершаются
    быстрее дедлайна — обход в to_thread под семафором (макс. 2), event
    loop не блокируется; повторный asyncio.run не роняет семафор."""
    async def _batch():
        t0 = time.monotonic()
        outs = await asyncio.gather(
            *[tools.source_grep(r"Записывать\(Истина\)") for _ in range(5)])
        return time.monotonic() - t0, outs

    elapsed, outs = asyncio.run(_batch())
    assert len(outs) == 5
    assert all(o["total_matches_returned"] == 2 for o in outs)
    assert all(o["files_scanned"] == 4 for o in outs)
    assert elapsed < tools._SOURCE_GREP_TIMEOUT
    # новый цикл — семафор привязывается заново, тул жив
    again = asyncio.run(tools.source_grep("Отложить"))
    assert again["files_scanned"] == 4


def test_no_source_root_is_error(tmp_path, monkeypatch):
    """Нет source_root (env не задан, активной конфигурации нет) → error."""
    monkeypatch.delenv("COMPASS_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("COMPASS_SETTINGS_PATH", raising=False)
    out = asyncio.run(tools.source_grep(r"Отложить"))
    assert "error" in out and "source_root" in out["error"]


def test_config_badge_from_registry(tmp_path, monkeypatch):
    """Активная конфигурация в реестре → config {id, short_name} в ответе."""
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    cid = add_config("ut", str(tmp_path / "src"))["id"]
    ws.save_settings({"default_config_id": cid})
    _write(tmp_path / "src", "CommonModules/Тест/Ext/Module.bsl",
           "ТекстСоСловомМаркер\n")
    try:
        out = asyncio.run(tools.source_grep("Маркер"))
        assert out["config"]["id"] == cid
        assert out["config"]["short_name"] == "ut"
    finally:
        ws._TEXT_CACHE.clear()
