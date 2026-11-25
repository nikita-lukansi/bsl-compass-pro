# -*- coding: utf-8 -*-
"""Контракт data_query (compass-xuy): принудительный лимит задокументирован
в описании MCP-тула (строковые ассерты по исходнику — образец
test_authorized_gate)."""
from pathlib import Path

_TOOLS = Path(__file__).parent.parent / "src" / "facade" / "runtime_tools.py"


def _data_query_doc(text: str) -> str:
    """Докстринг data_query: от 'def data_query(' до закрытия '''-блока."""
    start = text.index("def data_query(")
    doc_open = text.index('"""', start)
    doc_close = text.index('"""', doc_open + 3) + 3
    return text[start:doc_close]


def test_data_query_doc_documents_forced_limit():
    raw = _data_query_doc(_TOOLS.read_text(encoding="utf-8"))
    doc = " ".join(raw.split())   # фразы докстринга не должны зависеть от переносов
    # контракт: инжекция происходит даже при отсутствии лимита в тексте
    assert "принудительно ограничивает" in doc
    assert "ПЕРВЫЕ N+1" in doc
    # дефолт и источник «ПЕРВЫЕ 11» (limit=10 + строка детекции ЕщеЕсть)
    assert "ПЕРВЫЕ 11" in doc
    assert "ЕщеЕсть" in doc
    # фактический лимит виден по числу строк ответа
    assert "числу строк ответа" in doc
