# -*- coding: utf-8 -*-
"""compass-gbi: тела методов в индексе — fill_bodies/update_bodies и
ветка тел code_grep.

Герметично: мини-индекс + source tree из MODULE_SOURCES (тела есть только
у модулей 1–2; методы цепочки и расширений — skipped: файлов в выгрузке
нет). Отключение — env COMPASS_BODIES_FTS=off.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from src.recon import bodies  # noqa: E402
from tests.mini_index import (make_mini_db, make_source_tree)  # noqa: E402


@pytest.fixture
def mini_bodies(tmp_path, monkeypatch):
    """Мини-индекс + выгрузка; reader чист; env FTS-тела не задан."""
    monkeypatch.delenv("COMPASS_BODIES_FTS", raising=False)
    db = make_mini_db(tmp_path)
    root = make_source_tree(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    monkeypatch.setattr("src.context.active_config_id",
                        lambda: "Compass_mini_00000000")
    tools.reset_reader()
    yield db, root
    tools.reset_reader()


# ------------------------------------------------------------------ заливка

def test_fill_creates_tables_and_rows(mini_bodies):
    """Полная заливка: bodies=2 (модули с файлами), skipped=7 (без файлов),
    текст метода содержит строку объявления."""
    db, root = mini_bodies
    report = bodies.fill_bodies(db, root)
    assert report["status"] == "filled"
    assert report["bodies"] == 2 and report["skipped"] == 7
    conn = sqlite3.connect(db)
    text = conn.execute(
        "SELECT text FROM methods_bodies WHERE method_id = 1").fetchone()[0]
    assert "Функция ЭкспортнаяФункция(А) Экспорт" in text
    assert "Возврат А * 2;" in text
    # FTS-слой синхронен: запрос по телу находит
    hit = conn.execute(
        "SELECT rowid FROM methods_bodies_fts WHERE methods_bodies_fts "
        "MATCH 'Возврат'").fetchall()
    assert hit == [(1,)]
    conn.close()


def test_fill_idempotent_on_rebuild(mini_bodies):
    """Перезаливка после пересборки: старые строки (в т.ч. осиротевшие
    method_id) удаляются, счётчик стабильный."""
    db, root = mini_bodies
    assert bodies.fill_bodies(db, root)["bodies"] == 2
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO methods_bodies VALUES (4242, 'ЗАЛЕЖАВШИЙ')")
    conn.commit()
    conn.close()
    report = bodies.fill_bodies(db, root)
    assert report["bodies"] == 2
    conn = sqlite3.connect(db)
    stale = conn.execute(
        "SELECT 1 FROM methods_bodies WHERE method_id = 4242").fetchone()
    conn.close()
    assert stale is None


# ------------------------------------------------------------------- поиск

def test_code_grep_searches_bodies(mini_bodies):
    """Тела залиты → dict bodies_indexed:true; код-символьный запрос ищется;
    сниппет с подсветкой; нет совпадений — пустой results, не отказ."""
    db, root = mini_bodies
    bodies.fill_bodies(db, root)
    out = tools.code_grep("Возврат")
    assert isinstance(out, dict) and out["bodies_indexed"] is True
    assert out["total"] == 1
    row = out["results"][0]
    assert row["name"] == "ЭкспортнаяФункция" and row["snippet"]
    assert "[" in row["snippet"] and "]" in row["snippet"]
    # строка объявления входит в тело — имена ищутся тем же маршрутом
    name_hit = tools.code_grep("ОбработкаПроведения")
    assert name_hit["bodies_indexed"] is True
    assert name_hit["results"][0]["name"] == "ОбработкаПроведения"
    # нет совпадений — честная пустота
    none = tools.code_grep("ТакогоТокенаВКодеНетХХХ")
    assert none["bodies_indexed"] is True and none["results"] == []
    # битый FTS5-запрос — честный error
    bad = tools.code_grep("(")
    assert "error" in bad and "FTS5" in bad["error"]


def test_code_grep_bodies_module_hint(mini_bodies):
    """module_hint сужает ветку тел по rel_path."""
    db, root = mini_bodies
    bodies.fill_bodies(db, root)
    out = tools.code_grep("Возврат", module_hint="ДокЗаказ")
    assert out["total"] == 0
    out = tools.code_grep("Возврат", module_hint="ТестМодуль")
    assert out["total"] == 1


# ------------------------------------------------------------------ инкремент

def test_update_refills_changed_and_prunes_orphans(mini_bodies):
    """Инкремент: правка исходника → новый токен ищется; осиротевшие тела
    (метод убран из methods симуляцией gyrfalcon update) вычищаются."""
    db, root = mini_bodies
    bodies.fill_bodies(db, root)
    target = root / "CommonModules/ТестМодуль/Ext/Module.bsl"
    target.write_text(
        "#Область ПрограммныйИнтерфейс\n"
        "// Экспортная функция тестового модуля\n"
        "Функция ЭкспортнаяФункция(А) Экспорт\n"
        "\tВозврат А * 2; ИНКРЕМЕНТТОКЕН42;\n"
        "КонецФункции\n"
        "#КонецОбласти\n", encoding="utf-8-sig")
    report = bodies.update_bodies(db, root)
    assert report["status"] == "updated"
    out = tools.code_grep("ИНКРЕМЕНТТОКЕН42")
    assert out["bodies_indexed"] is True and out["total"] == 1
    assert out["results"][0]["name"] == "ЭкспортнаяФункция"

    # симуляция gyrfalcon update после удаления файла: методы убраны
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM methods WHERE module_id = 1")
    conn.commit()
    conn.close()
    report = bodies.update_bodies(db, root, changed=[])
    assert report["pruned"] >= 1
    assert tools.code_grep("Возврат")["total"] == 0
    assert tools.code_grep("ОбработкаПроведения")["total"] == 1


def test_update_first_run_falls_back_to_fill(mini_bodies):
    """Таблиц ещё нет → update_bodies разворачивается в полную заливку."""
    db, root = mini_bodies
    report = bodies.update_bodies(db, root)
    assert report["status"] == "filled" and report["bodies"] == 2


# ---------------------------------------------------------------- отключение

def test_disabled_env_keeps_old_behavior(mini_bodies, monkeypatch):
    """COMPASS_BODIES_FTS=off: заливка/инкремент отключены, code_grep —
    прежний name-only маршрут (код-паттерн → честный отказ)."""
    db, root = mini_bodies
    monkeypatch.setenv("COMPASS_BODIES_FTS", "off")
    assert bodies.fill_bodies(db, root)["status"] == "disabled"
    assert bodies.update_bodies(db, root)["status"] == "disabled"
    conn = sqlite3.connect(db)
    tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'methods_bodies'"
    ).fetchone()
    conn.close()
    assert tables is None, "отключенная заливка не создаёт таблиц"
    # именной запрос — старый список по methods_fts
    rows = tools.code_grep("Экспортн*", limit=10)
    assert not isinstance(rows, dict)
    assert "ЭкспортнаяФункция" in [x["name"] for x in rows
                                   if not x.get("_truncated")]
    # код-паттерн — прежний честный отказ
    out = tools.code_grep("Посчитать(Партия")
    assert out["bodies_indexed"] is False
    assert "тела методов не проиндексированы" in out["error"]


def test_code_grep_falls_back_without_bodies(mini_bodies):
    """Таблиц нет (старая сборка) — поведение как до compass-gbi."""
    db, root = mini_bodies
    out = tools.code_grep("Посчитать(Партия")
    assert out["bodies_indexed"] is False
