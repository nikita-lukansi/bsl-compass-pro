# -*- coding: utf-8 -*-
"""compass-4w7.5 (обратная связь 2026-11-15): два UX-фикса.

1. Хинт not_indexed не вводит в заблуждение на реквизитах шапки
   («Код» справочника получал совет про табличные части) — текст
   говорит, что ОТРИЦАТЕЛЬНЫЙ вердикт недоказуем, и называет оба
   варианта. Ветка v77 (условия появления not_indexed) не менялась.
2. Описание тула code_grep первым предложением сообщает границу индекса
   (compass-gbi: тела заливаются пост-шагом сборки; без тел — честный
   name-only fallback).
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent


def test_not_indexed_hint_is_honest(mini_reader):
    """v77-кейс: объект с атрибутами, но без индексированных секций ТЧ
    (БухгалтерскийГлавный: план счетов). Запрос реквизита шапки «Код» —
    сценарий обратки: не False, а честный not_indexed с новым хинтом,
    который не советует ТЧ там, где речь про шапку."""
    from src.facade import tools
    out = tools.verify_field("БухгалтерскийГлавный", "Код")
    assert out["holds"] is None and out["verdict"] == "not_indexed"
    assert "ОТРИЦАТЕЛЬНЫЙ вердикт недоказуем" in out["hint"]
    assert "реквизитом не в индексе" in out["hint"]
    assert "неиндексированной ТЧ" in out["hint"]


def test_old_misleading_phrase_gone():
    """Строковый гейт: старый хинт (совет про ТЧ на любом поле) удалён
    из исходника — по образцу test_authorized_gate.py."""
    text = (_ROOT / "src" / "facade" / "tools.py").read_text(encoding="utf-8")
    assert "может быть табличной частью" not in text


def test_code_grep_docstring_leads_with_boundary():
    """Первое предложение docstring code_grep — граница индекса
    (описание тула derive'ится из docstring); compass-gbi: тела заливаются
    пост-шагом сборки, name-only — честный fallback."""
    tree = ast.parse(
        (_ROOT / "src" / "facade" / "tools.py").read_text(encoding="utf-8"))
    doc = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "code_grep":
            doc = ast.get_docstring(node)
            break
    assert doc is not None
    assert doc.splitlines()[0] == \
        "Полнотекстовый поиск по коду: тела в индексе (compass-gbi), " \
        "иначе именный маршрут."
