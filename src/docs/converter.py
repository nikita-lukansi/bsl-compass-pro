# -*- coding: utf-8 -*-
"""Нарезка файлов глав ИТС на per-page HTML для kb sync.

Файл главы (/db/content/<db>/src/<файл>.htm) содержит главу целиком;
страница книги = от якоря до следующего якоря-границы (фактура §1б
спецификации). Границы двух видов: префиксные `<a name="Глава__Страница">`
и голые `<a name="Страница">` — вторыми служат только slug-и из набора
bare_pages (неразрешённые префиксно страницы), чтобы служебные ref-якоря
(_Ref…, IssOgl… — файловое оглавление) не дробили текст. Всё до первой
границы отбрасывается; картинки и скрипты вырезаются.
"""
from __future__ import annotations

import html as html_module
import re

# префиксный page-якорь: ровно одно двойное подчёркивание <Глава>__<Страница>;
# файловые якоря оглавления (IssOgl1_О_программе) и ref-якоря его не имеют
_PAGE_ANCHOR = re.compile(r'<a[^>]+name="([A-Za-z0-9]+__[A-Za-z0-9]+)"', re.I)
_ANCHOR_TAG = re.compile(r"<a\b[^>]*>", re.I)
_ID_ATTR = re.compile(r'\bid="([^"]+)"', re.I)
_NAME_ATTR = re.compile(r'\bname="([^"]+)"', re.I)
# числовые сущности (&#9632; — маркеры списков и т.п.): gyrfalcon декодирует
# только базовый именной набор (&amp; &lt; …), числовые остаются мусором в
# тексте; именные не трогаем — экранированные примеры кода (&lt;p&gt;) должны
# дожить до извлечения kb sync и не стать тегами
_NUMERIC_ENTITY = re.compile(r"&#(\d+);")
_IMG = re.compile(r"<img[^>]*>", re.I)
_ALT = re.compile(r'alt="([^"]*)"', re.I)
_BLOCKED = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAGLESS = re.compile(r"<[^>]+>")
_MULTISPACE = re.compile(r"\s+")


def anchor_index(chapter_html: str) -> list[tuple[str, str, int]]:
    """Все якоря файла: [(id_lower, name_lower, позиция)].

    У ITS-якорей парные атрибуты: id несёт slug страницы
    (<a id="retrobonus7" name="issogl1_…">), name — имя в файловом
    оглавлении или префиксный адрес <Гл>__<Стр>. Фрагмент iframe (#…) в
    ровно половине случаев указывает на id, поэтому смотреть надо оба.
    """
    out: list[tuple[str, str, int]] = []
    for match in _ANCHOR_TAG.finditer(chapter_html):
        tag = match.group(0)
        id_match = _ID_ATTR.search(tag)
        name_match = _NAME_ATTR.search(tag)
        anchor_id = (id_match.group(1).strip() if id_match else "").lower()
        anchor_name = (name_match.group(1).strip() if name_match else "").lower()
        if anchor_id or anchor_name:
            out.append((anchor_id, anchor_name, match.start()))
    return out


def _boundary_slug(anchor_id: str, anchor_name: str,
                   bare_pages: set[str]) -> str | None:
    """Slug-ключ границы или None: префиксный якорь <Гл>__<Стр> (по name
    или id) или голый slug страницы из набора bare_pages."""
    for cand in (anchor_name, anchor_id):
        if not cand:
            continue
        if "__" in cand and _PAGE_ANCHOR.match(f'<a name="{cand}">'):
            return cand
        if cand in bare_pages:
            return cand
    return None


def split_chapter(chapter_html: str,
                  bare_pages: set[str] | None = None) -> list[tuple[str, str]]:
    """Разрезать файл главы на сегменты страниц.

    Возвращает [(anchor_lower, segment_html), …] в порядке следования;
    anchor — ключ границы в нижнем регистре, segment — от якоря до
    следующей границы. bare_pages — slug-и страниц (нижний регистр),
    чьи голые якоря (по id или name) считать границами; служебные
    ref-якоря (_Ref…, IssOgl*) текст не дробят. Всё до первой границы
    (шапка и файловое оглавление) отбрасывается.
    """
    bare = {p.lower() for p in (bare_pages or set())}
    starts: list[tuple[str, int]] = []
    for anchor_id, anchor_name, pos in anchor_index(chapter_html):
        slug = _boundary_slug(anchor_id, anchor_name, bare)
        if slug is not None:
            starts.append((slug, pos))
    # одна позиция может быть границей дважды (id и name одной пары)
    seen_pos: set[int] = set()
    unique: list[tuple[str, int]] = []
    for slug, pos in sorted(starts, key=lambda sp: sp[1]):
        if pos in seen_pos:
            continue
        seen_pos.add(pos)
        unique.append((slug, pos))
    result = []
    for i, (anchor, start) in enumerate(unique):
        end = unique[i + 1][1] if i + 1 < len(unique) else len(chapter_html)
        result.append((anchor, chapter_html[start:end]))
    return result


def clean_segment(segment_html: str) -> str:
    """Вычистить сегмент: скрипты/стили/комментарии/картинки.

    Картинки заменяются на «(рисунок: alt)» при непустом alt — контекст
    рисунка сохраняется для поиска, бинарные payload не тянутся (kb sync
    не пропускает их в извлечённом тексте, диск не раздувается).
    """
    def image_repl(match: re.Match) -> str:
        alt = _ALT.search(match.group(0))
        label = (alt.group(1).strip() if alt else "").strip()
        return f" (рисунок: {html_module.escape(label)}) " if label else " "

    cleaned = _BLOCKED.sub(" ", segment_html)
    cleaned = _COMMENT.sub(" ", cleaned)
    cleaned = _IMG.sub(image_repl, cleaned)
    cleaned = _NUMERIC_ENTITY.sub(
        lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000
        else " ", cleaned)
    return cleaned.strip()


def page_document(title: str, segment_html: str) -> str:
    """Собрать per-page HTML: заголовок из дерева + очищенное тело.

    kb sync берёт title из первой строки извлечённого текста, поэтому
    заголовок дерева ставится первым (точнее, чем внутренний h2/h3 главы);
    первый внутренний заголовок с тем же текстом снимается, чтобы не
    дублировать его в извлечённом тексте страницы.
    """
    segment_html = clean_segment(segment_html)
    if title:
        # первый заголовок в начале сегмента (там ещё якорь <a name=…>)
        head_zone = segment_html[:200]
        first_heading = re.search(
            r"<h([1-6])[^>]*>(.*?)</h\1>", head_zone, re.S | re.I)
        if first_heading:
            inner = _MULTISPACE.sub(" ", _TAGLESS.sub("", first_heading.group(2)))
            if inner.strip().casefold() == title.casefold():
                segment_html = (segment_html[:first_heading.start()]
                                + segment_html[first_heading.end():])
    return (f"<h1>{html_module.escape(title)}</h1>\n"
            f"{segment_html.strip()}\n")
