# -*- coding: utf-8 -*-
"""Тесты краулера ИТС: дерево → главы → файлы → нарезка (фейковый клиент).

Сетевой код — через injectable client: ItsClient подменяется на объект с
заготовленными ответами (паттерн injectable opener из спецификации).
"""
import json
import sys
import urllib.parse
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.docs import crawler  # noqa: E402
from src.docs.converter import clean_segment, page_document, split_chapter  # noqa: E402
from src.docs.its_client import BASE, ItsError  # noqa: E402

_INDEX_PAGE = ('<div id="w_metadata_toc" '
               'data-url="/db/metadata/ka25doc/toc/572_?_=1"></div>')

_TREE = """
<ul class="tree top active mixed tid">
 <li class="expanded active" id="toc_572_index">
   <a href="/db/ka25doc/bookmark/Index/Index">КА 2.5</a></li>
 <li class="collapsed" id="toc_573_introduction">
   <a href="/db/ka25doc/bookmark/Introduction/Introduction">Введение</a>
   <ul>
    <li id="toc_573_about">
      <a href="/db/ka25doc/bookmark/Introduction/About">О программе</a></li>
    <li id="toc_573_about2">
      <a href="/db/ka25doc/bookmark/Introduction/About">О программе</a></li>
    <li id="toc_573_demobase">
      <a href="/db/ka25doc/bookmark/Introduction/DemoBase">Демобаза</a></li>
   </ul>
 </li>
 <li class="collapsed" id="toc_900_old">
   <a href="/db/ka25doc/bookmark/Old/One">Старое</a>
   <ul>
    <li id="toc_900_two">
      <a href="/db/ka25doc/bookmark/Old/Two">Вторая</a></li>
   </ul>
 </li>
</ul>
"""

_CHAPTER = """<html><body>
<a name="IssOgl1_О_программе">Оглавление</a>
<a name="Introduction__Introduction"><h2>Введение</h2></a>
<p>Первый текст введения.</p>
<a name="Introduction__About"><h2>О программе</h2></a>
<p>Текст о программе с <img src="data:image/png;base64,AAAA" alt="схема"> картинкой.</p>
<a name="Introduction__DemoBase"><h2>Демобаза</h2></a>
<p>Демобаза готова.</p>
</body></html>"""

_INDEX_CHAPTER = "<html><body><p>Корневой указатель без page-якорей.</p></body></html>"

_OLD1 = ('<a id="one" class="bookmark" name="issogl1_старое"></a>'
         '<h2>Старое</h2><p>Первая страница старой главы.</p>')
_OLD2 = ('<a name="Old__Two"><h2>Вторая</h2></a>'
         '<p>Вторая страница — в другом файле главы.</p>')


class FakeClient:
    """Клиент с заготовленными ответами; фиксирует запросы."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.responses:
            raise ItsError(f"нет заготовки: {url}")
        return self.responses[url]


def _fake(monkeypatch, tmp_path):
    """Краулер с фейковым клиентом и корпусом во tmp."""
    responses = {
        f"{BASE}/db/ka25doc": _INDEX_PAGE,
        f"{BASE}/db/metadata/ka25doc/toc/572_?_=1": _TREE,
        f"{BASE}/db/ka25doc/bookmark/Index/Index":
            '<iframe src="/db/content/ka25doc/src/index.htm"></iframe>',
        f"{BASE}/db/ka25doc/bookmark/Introduction/Introduction":
            '<iframe src="/db/content/ka25doc/src/введение.htm"></iframe>',
        f"{BASE}/db/ka25doc/bookmark/Old/One":
            '<iframe src="/db/content/ka25doc/src/old1.htm"></iframe>',
        f"{BASE}/db/ka25doc/bookmark/Old/Two":
            '<iframe src="/db/content/ka25doc/src/old2.htm#TWO"></iframe>',
        f"{BASE}/db/content/ka25doc/src/index.htm": _INDEX_CHAPTER,
        f"{BASE}/db/content/ka25doc/src/"
        f"{urllib.parse.quote('введение.htm')}": _CHAPTER,
        f"{BASE}/db/content/ka25doc/src/old1.htm": _OLD1,
        f"{BASE}/db/content/ka25doc/src/old2.htm": _OLD2,
    }
    client = FakeClient(responses)
    monkeypatch.setenv("WORKSPACE_HUB", str(tmp_path))
    return client, crawler.corpus_root("ka25doc")


def test_split_chapter_boundaries():
    """Страница = от якоря до следующего якоря; шапка с оглавлением отброшена."""
    segments = split_chapter(_CHAPTER)
    anchors = [a for a, _ in segments]
    assert anchors == ["introduction__introduction", "introduction__about",
                       "introduction__demobase"]
    assert "Оглавление" not in segments[0][1], "IssOgl-хвост не попал в страницу"


def test_split_chapter_bare_anchors():
    """Голые якоря (без префикса главы) — границы только из набора bare_pages;
    служебные ref-якоря текст не дробят."""
    chapter = ('<a name="Batches"><h2>Серии</h2></a><p>Корень главы.</p>'
               '<a name="_Ref123">ссылка</a>'
               '<a name="BatchRegistrationPrinciples"><h2>Принцип</h2></a>'
               '<p>Текст принципа.</p>'
               '<a name="Batches__BatchPolitics"><h2>Политика</h2></a>'
               '<p>Текст политики.</p>')
    segments = dict(split_chapter(
        chapter, bare_pages={"Batches", "BatchRegistrationPrinciples"}))
    assert list(segments) == ["batches", "batchregistrationprinciples",
                              "batches__batchpolitics"]
    assert "Корень главы" in segments["batches"]
    assert "_Ref123" in segments["batches"], "ref-якорь не граница"
    assert "Текст принципа" in segments["batchregistrationprinciples"]
    # без набора — только префиксные якоря, голая зона отброшена
    only_prefixed = dict(split_chapter(chapter))
    assert list(only_prefixed) == ["batches__batchpolitics"]


def test_clean_segment_strips_media():
    seg = '<p><img src="data:image/png;base64,AA" alt="схема"> текст</p>'
    cleaned = clean_segment(seg)
    assert "data:image" not in cleaned and "base64" not in cleaned
    assert "(рисунок: схема)" in cleaned


def test_page_document_dedupes_first_heading():
    doc = page_document("О программе", '<a name="X"> </a><h2>О программе</h2><p>т</p>')
    assert doc.count("О программе") == 1, "дубль заголовка снят"


def test_parse_tree_order_dedupe():
    items = crawler.parse_tree(_TREE, "ka25doc")
    assert [(it.chapter, it.page) for it in items] == [
        ("Index", "Index"), ("Introduction", "Introduction"),
        ("Introduction", "About"), ("Introduction", "DemoBase"),
        ("Old", "One"), ("Old", "Two")]
    assert items[2].title == "О программе"


def test_parse_tree_filters_other_db():
    items = crawler.parse_tree(
        '<a href="/db/bp8doc/bookmark/A/B">Чужое</a>' + _TREE, "ka25doc")
    assert all(it.chapter != "A" for it in items)


def test_find_toc_url_from_data_url():
    url = crawler.find_toc_url(_INDEX_PAGE, "ka25doc")
    assert url == f"{BASE}/db/metadata/ka25doc/toc/572_?_=1"


def test_run_crawl_full_pipeline(monkeypatch, tmp_path):
    client, root = _fake(monkeypatch, tmp_path)
    stats = crawler.run_crawl(client, "ka25doc", root=root)
    # все 6 страниц написаны: Index/Index — голова файла (корень главы),
    # Old/Two — по фрагменту #TWO из bookmark-страницы
    assert stats["pages_written"] == 6
    assert stats["missing"] == []
    two = (root / "Old" / "Two" / "Ext" / "Help" / "ru.html")
    assert "другом файле" in two.read_text(encoding="utf-8")
    # голый якорь: Old/One в old1.htm назван <a name="One"> без префикса
    one = (root / "Old" / "One" / "Ext" / "Help" / "ru.html")
    assert "Первая страница" in one.read_text(encoding="utf-8")
    page = (root / "Introduction" / "About" / "Ext" / "Help" / "ru.html")
    assert "О программе" in page.read_text(encoding="utf-8")
    assert "base64" not in page.read_text(encoding="utf-8")
    state = json.loads((root / "_state" / "sync_state.json")
                       .read_text(encoding="utf-8"))
    assert set(state["chapter_files"]) == {"Index", "Introduction", "Old"}
    assert state["page_files"]["Index/Index"] == {
        "file": "index.htm", "fragment": ""}
    # resolve: bookmark на первую страницу каждой главы (3) +
    # fallback: Index/Index и Old/Two (2)
    bookmarks = [c for c in client.calls if "/bookmark/" in c]
    files = [c for c in client.calls if "/db/content/" in c]
    assert len(bookmarks) == 5 and len(files) == 4


def test_run_crawl_incremental_no_network(monkeypatch, tmp_path):
    """Второй прогон по неизменённому корпусу: только переоткрытие дерева;
    файлы и bookmark-запросы не повторяются (всё резолвится локально)."""
    client, root = _fake(monkeypatch, tmp_path)
    crawler.run_crawl(client, "ka25doc", root=root)
    client.calls.clear()
    stats = crawler.run_crawl(client, "ka25doc", root=root)
    content = [c for c in client.calls if "/db/content/" in c]
    assert content == [], "файлы глав уже в состоянии"
    assert len(client.calls) <= 2, "только переоткрытие книги и дерева"
    # old2 из fallback-фазы счётчик unchanged не ведёт — только файлы глав
    assert stats["files_unchanged"] == 3 and stats["files_fetched"] == 0
    assert stats["missing"] == []


def test_run_crawl_limit_chapters(monkeypatch, tmp_path):
    client, root = _fake(monkeypatch, tmp_path)
    stats = crawler.run_crawl(client, "ka25doc", root=root, limit_chapters=1)
    assert stats["pages_total"] == 1, "первые N глав дерева"


def test_corpus_root_env():
    import os
    saved = os.environ.get("WORKSPACE_HUB")
    try:
        os.environ["WORKSPACE_HUB"] = "D:/hub"
        assert crawler.corpus_root("ka25doc") == Path("D:/hub/Help/ka25doc")
        del os.environ["WORKSPACE_HUB"]
        assert crawler.corpus_root("bp8doc") == Path("/workspace/Help/bp8doc")
    finally:
        if saved is not None:
            os.environ["WORKSPACE_HUB"] = saved
        else:
            os.environ.pop("WORKSPACE_HUB", None)


def test_corpus_root_cid_docs_hub(tmp_path, monkeypatch):
    """S1/S5: corpus_root с cid — docs-хаб конфигурации; без cid — legacy
    точка входа host-CLI (WORKSPACE_HUB/Help или /workspace/Help)."""
    import os
    from src.docs.crawler import corpus_root
    from src.registry import config_docs_root
    monkeypatch.setenv("COMPASS_DOCS_ROOT", str(tmp_path / "data" / "docs"))
    assert corpus_root("ut11doc", "Compass_k_1") ==         config_docs_root("Compass_k_1", "ut11doc")
    monkeypatch.delenv("COMPASS_DOCS_ROOT", raising=False)
    os.environ["WORKSPACE_HUB"] = str(tmp_path)
    try:
        assert corpus_root("ka25doc") == tmp_path / "Help" / "ka25doc"
    finally:
        os.environ.pop("WORKSPACE_HUB", None)
    assert corpus_root("ka25doc") == Path("/workspace/Help/ka25doc")
