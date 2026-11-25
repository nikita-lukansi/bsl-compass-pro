# -*- coding: utf-8 -*-
"""Тулы расширений Т3: эффективный поток (arch §5), dev-скрытие (§8),
директива extension_add (v0.4), сверка стека с индексом."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import registry as registry_mod  # noqa: E402
from src import workspace as ws  # noqa: E402
from src.facade import extension_tools as ext  # noqa: E402
from src.facade import tools as facade_tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def ext_env(tmp_path, monkeypatch):
    """Мини-индекс Т3-K + workspace-реестр в tmp + активная конфигурация."""
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(facade_tools, "active_index_db", lambda: db)
    facade_tools.reset_reader()
    settings_dir = tmp_path / "Settings"
    settings_dir.mkdir()
    monkeypatch.setenv("COMPASS_SETTINGS_PATH", str(settings_dir))
    ws._TEXT_CACHE.clear()
    cid = registry_mod.add_config("KA", str(tmp_path / "src"))["id"]
    settings = ws.load_settings()
    settings["default_config_id"] = cid
    ws.save_settings(settings)
    yield {"cid": cid}
    facade_tools.reset_reader()


def _build_stack(cid: str) -> None:
    """Стек как в arch v0.5: Расш1(Patch) → Расш2(Customization) → Расш5(Addition)."""
    registry_mod.stack_add(cid, "Расш1", 1, "Patch")
    registry_mod.stack_add(cid, "Расш2", 1, "Customization")
    registry_mod.stack_add(cid, "Расш5", 1, "Addition")


def test_effective_method_flow_order(ext_env):
    cid = ext_env["cid"]
    _build_stack(cid)
    out = ext.effective_method("ЭкспортнаяФункция")
    # Расш5 (Addition) — верхний слой: его &Вместо активный; Расш2 — shadowed
    instead = next(b for b in out["flow"] if b["kind"] == "instead")
    assert instead["source"] == "Расш5"
    assert {s["source"] for s in instead["shadowed"]} == {"Расш2"}
    # &Перед верхнего слоя исполняется первым
    assert out["flow"][0]["kind"] == "before" and out["flow"][0]["source"] == "Расш1"
    assert out["order_confirmed"] is False          # manual-стек
    # атрибуция body: базовый модуль (rel_path без ../ext)
    body = next(b for b in out["flow"] if b["kind"] == "body")
    assert body["source"] == "base"
    assert not (body["rel_path"] or "").startswith("../")


def test_dev_hidden_and_include_dev(ext_env):
    """Расш5 → dev: активный &Вместо теперь Расш2; без include_dev —
    dev_hidden=1 и Расш5 не в потоке; с include_dev — показан с пометкой."""
    cid = ext_env["cid"]
    _build_stack(cid)
    registry_mod.stack_set_status(cid, "Расш5", "dev")
    out = ext.effective_method("ЭкспортнаяФункция")
    assert out["dev_hidden"] == 1
    instead = next(b for b in out["flow"] if b["kind"] == "instead")
    assert instead["source"] == "Расш2" and not instead["shadowed"]
    assert all(b.get("source") != "Расш5" for b in out["flow"])
    out2 = ext.effective_method("ЭкспортнаяФункция", include_dev=True)
    instead2 = next(b for b in out2["flow"] if b["kind"] == "instead")
    assert instead2["source"] == "Расш5" and instead2.get("dev") is True
    assert {s["source"] for s in instead2["shadowed"]} == {"Расш2"}


def test_dev_hidden_in_who_overrides(ext_env):
    """compass-941.4 (п.1в): dev-тени в who_overrides — скрытие по умолчанию
    и показ с include_dev; скрытый верхний слой НЕ блокирует active Расш2."""
    cid = ext_env["cid"]
    _build_stack(cid)
    registry_mod.stack_set_status(cid, "Расш5", "dev")
    out = ext.who_overrides("ЭкспортнаяФункция")
    assert out["dev_hidden"] == 1
    names = [o["extension_name"] for o in out["overrides"]]
    assert "Расш5" not in names and set(names) == {"Расш1", "Расш2"}
    instead = next(o for o in out["overrides"] if o["kind"] == "instead")
    # Расш5 скрыт → активный верхний слой для &Вместо — Расш2, не затенён
    assert instead["extension_name"] == "Расш2"
    assert instead["active"] is True and not instead.get("shadowed")
    # показ: оба &Вместо на месте, dev-пометка, активен верхний (Расш5)
    out2 = ext.who_overrides("ЭкспортнаяФункция", include_dev=True)
    names2 = [o["extension_name"] for o in out2["overrides"]]
    assert set(names2) == {"Расш1", "Расш2", "Расш5"}
    insteads2 = {o["extension_name"]: o
                 for o in out2["overrides"] if o["kind"] == "instead"}
    assert insteads2["Расш5"]["dev"] is True and insteads2["Расш5"]["active"]
    assert insteads2["Расш2"]["shadowed"] is True


def test_dev_only_overrides_hidden(ext_env):
    """compass-941.4 (п.1в): ВСЕ перехваты метода в dev-расширениях —
    честное _no_matches с dev_hidden, а не пустой поток."""
    cid = ext_env["cid"]
    _build_stack(cid)
    for name in ("Расш1", "Расш2", "Расш5"):
        registry_mod.stack_set_status(cid, name, "dev")
    out = ext.who_overrides("ЭкспортнаяФункция")
    assert out.get("_no_matches") is True and out["dev_hidden"] == 3
    assert "include_dev" in out["hint"]
    out2 = ext.effective_method("ЭкспортнаяФункция")
    # поток без перехватов: только body, dev_hidden честно назван
    assert out2["dev_hidden"] == 3
    kinds = {b["kind"] for b in out2["flow"]}
    assert kinds == {"body"}
    out3 = ext.effective_method("ЭкспортнаяФункция", include_dev=True)
    assert out3["dev_hidden"] == 0
    assert any(b["kind"] == "instead" for b in out3["flow"])


def test_extension_add_requires_questions(ext_env):
    out = ext.extension_add("Расш9")            # без ничего → подсказка
    dumped = json.dumps(out, ensure_ascii=False).lower()
    assert "подсказк" in dumped or "hint" in out
    ok = ext.extension_add("Расш9", 2, "Дополнение")   # русское назначение
    assert "error" not in ok
    assert registry_mod.stack_of(ext_env["cid"])[-1]["name"] == "Расш9"


def test_stack_mismatch_warnings(ext_env):
    """В фикстуре 3 расширения, стек пуст → предупреждение «добавьте через
    extension_add» для каждого имени + счётчики из индекса."""
    out = ext.extension_stack()
    idx_only = [w for w in out["warnings"] if w["kind"] == "index_only"]
    assert {w["extension"] for w in idx_only} == {"Расш1", "Расш2", "Расш5"}
    assert all("добавьте через extension_add" in w["text"] for w in idx_only)
    assert out["order_confirmed"] is False
    counts = {e["name"]: e for e in out["extensions"]}
    assert counts["Расш1"]["overrides"] == 1
    assert counts["Расш2"]["overrides"] == 1
    assert counts["Расш5"]["overrides"] == 1
    assert counts["Расш1"]["borrowed"] == 1


def test_effective_method_empty_stack_honest(ext_env):
    """Без стека — перечень перехватов без порядка + честный хинт."""
    out = ext.effective_method("ЭкспортнаяФункция")
    assert out["order_confirmed"] is False
    assert len(out["overrides"]) == 3 and "hint" in out


def test_who_overrides_active_shadowed(ext_env):
    cid = ext_env["cid"]
    _build_stack(cid)
    out = ext.who_overrides("ЭкспортнаяФункция")
    instead = [o for o in out["overrides"] if o["kind"] == "instead"]
    active = [o for o in instead if o["active"]]
    shadowed = [o for o in instead if o.get("shadowed")]
    assert len(active) == 1 and active[0]["extension_name"] == "Расш5"
    assert [o["extension_name"] for o in shadowed] == ["Расш2"]
    before = next(o for o in out["overrides"] if o["kind"] == "before")
    assert "active" not in before                  # только &Вместо имеет active


def test_no_matches_without_overrides(ext_env):
    out = ext.effective_method("ОбработкаПроведения")
    assert out.get("_no_matches") is True
    out2 = ext.who_overrides("ОбработкаПроведения")
    assert out2.get("_no_matches") is True


# --------------------------------------------------- Т3-O: extension_map + стек

def test_extension_map_overview_with_stack(ext_env):
    """Обзор с непустым стеком: effective_position по сортировке v0.5."""
    cid = ext_env["cid"]
    _build_stack(cid)
    out = facade_tools.extension_map()
    exts = {e["name"]: e for e in out["extensions"]}
    assert exts["Расш1"]["effective_position"] == 1   # Patch — первым
    assert exts["Расш2"]["effective_position"] == 2
    assert exts["Расш5"]["effective_position"] == 3
    assert exts["Расш1"]["in_stack"] is True
    assert exts["Расш1"]["purpose"] == "Patch"
    assert out["order_confirmed"] is False


def test_extension_map_overview_index_only_hint(ext_env):
    """Расширение индекса вне стека — in_stack: false + хинт."""
    out = facade_tools.extension_map()
    assert all(e["in_stack"] is False for e in out["extensions"])
    assert all("extension_add" in e["hint"] for e in out["extensions"])


def test_extension_map_details_with_stack(ext_env):
    _build_stack(ext_env["cid"])
    out = facade_tools.extension_map("ОбъектТест")
    rows = {r["extension_name"]: r for r in out["overrides"]}
    assert rows["Расш1"]["effective_position"] == 1
    assert rows["Расш5"]["active"] is True
    assert rows["Расш2"]["shadowed"] is True and rows["Расш2"]["active"] is False
    assert out["order_confirmed"] is False


def test_extension_map_details_without_stack(ext_env):
    out = facade_tools.extension_map("ОбъектТест")
    assert out["order_confirmed"] is False
    assert all("effective_position" not in r for r in out["overrides"])


def test_sync_stack_from_ping(ext_env):
    """Ф4: registry-синк — сверка состава/версий; расширения из базы без
    регистрации НЕ добавляются (директива v0.4); позиции не трогаются."""
    cid = ext_env["cid"]
    _build_stack(cid)
    ping = [
        {"Имя": "Расш1", "Версия": "1.2.0", "Активно": True},
        {"Имя": "Расш2", "Версия": "2.0.0", "Активно": True},
        {"Имя": "расш5", "Версия": "0.5.0", "Активно": False},  # регистр + неактивно
        {"Имя": "Расш9", "Версия": "1.0.0", "Активно": True},   # нет в стеке
    ]
    report = registry_mod.sync_stack_from_ping(cid, ping)
    # имена в matched — как в Пинге (база может вернуть иной регистр)
    assert sorted(report["matched"]) == ["Расш1", "Расш2", "расш5"]
    assert report["missing_in_stack"] == [{"name": "Расш9", "version": "1.0.0"}]
    assert report["inactive_in_base"] == ["расш5"]
    # версии обновились в записях стека, позиции не тронуты
    entry = registry_mod.get_config(cid)
    by_name = {e["name"]: e for e in entry["stack"]}
    assert by_name["Расш1"]["version"] == "1.2.0"
    assert by_name["Расш1"]["position"] == 1
    assert by_name["Расш1"]["order_source"] == "manual"  # честность §9
    assert by_name["Расш5"]["synced_at"]
    assert entry["stack_synced_at"] == report["synced_at"]
    assert "ПорядокДоступен=Ложь" in report["order_note"]

    # расширение из стека, исчезнувшее из базы
    report2 = registry_mod.sync_stack_from_ping(
        cid, [{"Имя": "Расш1", "Версия": "1.2.0", "Активно": True}])
    assert report2["missing_in_base"] == ["Расш2", "Расш5"]

    # неизвестная конфигурация
    assert "error" in registry_mod.sync_stack_from_ping("Compass_xxx_00000000", ping)


def test_extension_sync_from_ping_explicit(ext_env):
    """Ф4: тул с готовым ping_result — без сети; order_available=false."""
    cid = ext_env["cid"]
    registry_mod.stack_add(cid, "Расш1", 1, "Patch")
    ping = {"Версия": "0.2.0", "ПорядокДоступен": False,
            "Расширения": [{"Имя": "Расш1", "Версия": "3.0.0", "Активно": True},
                           {"Имя": "Расш7", "Версия": "", "Активно": True}]}
    report = ext.extension_sync_from_ping(ping_result=ping, cid=cid)
    assert report["matched"] == ["Расш1"]
    assert report["missing_in_stack"] == [{"name": "Расш7", "version": ""}]
    assert report["order_available"] is False


def test_extension_sync_ping_without_extensions(ext_env):
    """Пинг с ошибкой расширений (РасширенияОшибка) → честная ошибка."""
    report = ext.extension_sync_from_ping(
        ping_result={"РасширенияОшибка": "права нет"}, cid=ext_env["cid"])
    assert "error" in report and "права нет" in report["error"]


def test_extension_sync_offline_hint(ext_env, monkeypatch):
    """Офлайн-режим без ping_result — подсказка про offline_submit."""
    from src.facade import runtime_tools
    monkeypatch.setattr(runtime_tools, "_offline_mode", lambda: True)
    report = ext.extension_sync_from_ping(cid=ext_env["cid"])
    assert "hint" in report and "offline_submit" in report["hint"]


# ------------------------------------------------------------- Т4: досье

@pytest.fixture
def dossier_env(ext_env):
    """ext_env + мини-индекс как активный (досье строится по entry.index_db)."""
    cid = ext_env["cid"]
    entry = registry_mod.get_config(cid)
    entry["index_db"] = str(facade_tools.active_index_db())
    # запись реестра уже сохранена add_config; обновим индекс-путь
    reg = registry_mod.load_registry()
    reg["configs"][cid]["index_db"] = ext_env.get("db", "")
    registry_mod.save_registry(reg)
    return ext_env


def test_dossier_build_and_read(ext_env, tmp_path):
    """Т4: каркас из мини-индекса — подсистемы, объекты, справка, точки
    входа; get_docs читает; rebuild не затирает оверлеи."""
    from src.facade import dossier
    # реестр: index_db → мини-база фикстуры
    cid = ext_env["cid"]
    db_path = facade_tools.active_index_db()
    reg = registry_mod.load_registry()
    reg["configs"][cid]["index_db"] = str(db_path)
    registry_mod.save_registry(reg)

    report = dossier.build_docs(cid)
    assert "written" in report and report["objects_total"] >= 3
    assert any("Продажи" in s for s in report["sections"])
    text = Path(report["written"]).read_text(encoding="utf-8")
    assert "ДокЗаказ" in text and "Заказ клиента" in text
    assert "ОбработкаПроведения" in text          # точка входа
    assert "Ext/Help/ru.html (primary)" in text    # справка primary
    assert "Тестовый объект" in text               # синоним ОбъектТест

    # оверлей владельца переживает регенерацию
    overlay = Path(report["written"]).parent / "notes-учёт.md"
    overlay.write_text("# Заметка владельца\n Особенность склада\n",
                       encoding="utf-8")
    report2 = dossier.build_docs(cid)
    assert Path(report2["written"]).exists()
    assert overlay.exists() and "склада" in overlay.read_text(encoding="utf-8")

    got = dossier.get_docs(cid)
    assert got["overlays"] == ["notes-учёт.md"]
    sec = dossier.get_docs(cid, section="Подсистема: Продажи")
    assert "ДокЗаказ" in sec["text"]


def test_dossier_search(ext_env):
    """search_dossier: подстрока по каркасу и оверлеям, честная пустота."""
    from src.facade import dossier
    cid = ext_env["cid"]
    db_path = facade_tools.active_index_db()
    reg = registry_mod.load_registry()
    reg["configs"][cid]["index_db"] = str(db_path)
    registry_mod.save_registry(reg)
    dossier.build_docs(cid)
    hits = dossier.search_dossier("Заказ клиента")
    assert hits and hits[0]["file"] == "dossier.md"
    assert hits[0]["section"].startswith("Подсистема")

    miss = dossier.search_dossier("НетТакогоНигдеХХХ")
    assert miss[0]["_no_matches"] is True


def test_dossier_errors(ext_env):
    """Честные ошибки: несуществующая конфигурация; пустой запрос."""
    from src.facade import dossier
    assert "error" in dossier.build_docs("Compass_xxx_00000000")
    assert "error" in dossier.get_docs("Compass_xxx_00000000")
    assert "error" in dossier.search_dossier("")
