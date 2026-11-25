# -*- coding: utf-8 -*-
"""TASK-23: фильтры predefined_items — query-подстрока и лимит с маркером."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.facade import tools  # noqa: E402


def test_predefined_filter_by_code(mini_reader):
    out = tools.predefined_items("Счета", query="41")
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["item_code"] == "41"


def test_predefined_truncation_marker(mini_reader):
    out = tools.predefined_items("ВидыСубконтоТест", limit=2)
    assert out[-1].get("_truncated") is True and out[-1]["total"] == 3


def test_predefined_no_filter_returns_all(mini_reader):
    out = tools.predefined_items("ВидыСубконтоТест")
    assert len(out) == 3
