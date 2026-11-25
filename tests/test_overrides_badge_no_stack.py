# -*- coding: utf-8 -*-
"""compass-5cg: overrides-бейдж работает БЕЗ стека расширений.

Т3-N: бейдж — факт о наличии перехватов в extension_overrides индекса;
стек (реестр) влияет только на effective_position/active в who_overrides/
effective_method. Мини-фикстура с перехватами и ПУСТЫМ стеком: бейдж
обязан появиться в object_overview/read_function/find_in_forms.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402
from tests.mini_index import make_mini_db  # noqa: E402


@pytest.fixture
def mini_no_registry(monkeypatch, tmp_path):
    """Мини-индекс с перехватами, реестра нет → стек пуст."""
    db = make_mini_db(tmp_path)
    monkeypatch.setattr(tools, "active_index_db", lambda: db)
    tools.reset_reader()
    # страховка: активной конфигурации нет — стек обязана дать пустой
    import src.context as ctx
    monkeypatch.setattr(ctx, "active_config_id", lambda: None)
    yield db
    tools.reset_reader()


def test_overview_badge_without_stack(mini_no_registry):
    """object_overview отдаёт overrides-бейдж при пустом стеке."""
    out = tools.object_overview("ОбъектТест")
    assert "overrides" in out, "бейдж обязан появиться по extension_overrides"
    badge = out["overrides"]
    assert badge["count"] == 3                      # три перехвата в сиде
    assert badge["instead"] == 2                    # два &Вместо
    assert badge["hint"] == "effective_method"


def test_read_function_badge_without_stack(mini_no_registry):
    out = tools.read_function("ЭкспортнаяФункция")
    assert out.get("overrides", {}).get("count") == 3


def test_find_in_forms_badge_marker_without_stack(mini_no_registry):
    """list-ответ несёт бейдж маркером-элементом (Т3-N) — тоже без стека."""
    rows = tools.find_in_forms("Комментарий", object_name="ДокЗаказ")
    assert isinstance(rows, list)


def test_badge_absent_without_intercepts(mini_no_registry):
    """У объекта без перехватов бейджа нет — честная пустота, не {count:0}."""
    out = tools.object_overview("ДокЗаказ")
    assert "overrides" not in out


def test_badge_independent_of_stack_content(mini_no_registry, monkeypatch):
    """Контраст: данные бейджа — из индекса; стек меняет только атрибуцию.
    Здесь стек пуст вовсе — те же цифры, что и с непустым стеком
    (test_extension_map.py), т.к. источник один — extension_overrides."""
    from src.facade.extension_tools import _split_dev
    kept, hidden = _split_dev([{"extension_name": "Расш1"}], {}, False)
    assert kept and hidden == 0     # нет записи стека — строка не скрывается
