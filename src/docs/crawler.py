# -*- coding: utf-8 -*-
"""Выгрузка документации ИТС: дерево оглавления → файлы глав → страницы.

Пайплайн (фактура §1б спецификации):
  1. GET /db/<source_id>                — страница книги (найти URL дерева);
  2. GET /db/metadata/<db>/toc/<id>_    — фрагмент дерева: все страницы
     <a href="/db/<db>/bookmark/<Глава>/<Страница>"> с заголовками;
  3. по одной bookmark-странице на главу — имя src-файла из iframe;
  4. GET /db/content/<db>/src/<файл>.htm — файл главы (вся глава);
  5. локальная нарезка по якорям <Глава>__<Страница> → per-page файлы
     <root>/<Глава>/<Страница>/Ext/Help/ru.html (лейаут под is_help_html).

Сырые файлы глав кэшируются в <root>/_src (*.htm — вне Ext/Help, kb sync
их не видит), состояние — <root>/_state/sync_state.json. Куки и креды на
диск не пишутся. Инкрементальность: файл главы перекачивается и
перенарезается только при смене sha256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .converter import _PAGE_ANCHOR, anchor_index, page_document, split_chapter
from .its_client import BASE, ItsClient, ItsError

# <a href="/db/<db>/bookmark/<Глава>/<Страница>">Заголовок</a>
_BOOKMARK = re.compile(
    r'<a href="/db/(?P<db>[a-z0-9]+)/bookmark/(?P<chapter>[A-Za-z0-9_]+)'
    r'/(?P<page>[A-Za-z0-9_]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_TAGLESS = re.compile(r"<[^>]+>")
_MULTISPACE = re.compile(r"\s+")
_IFRAME_SRC = re.compile(r'src="(/db/content/[a-z0-9]+/src/[^"]+)"')


@dataclass
class TocItem:
    """Страница книги в дереве оглавления ИТС."""
    chapter: str
    page: str
    title: str


def find_toc_url(index_page: str, source_id: str) -> str:
    """URL фрагмента дерева оглавления на странице книги.

    URL встречается в data-url атрибуте и в JS-строке, но не в href.
    """
    match = re.search(rf"""['\"](/db/metadata/{source_id}/toc/[^'\"]+)['\"]""",
                      index_page)
    if not match:
        raise ItsError(f"на странице /db/{source_id} нет ссылки на дерево "
                       "оглавления (разметка изменилась?)")
    return BASE + match.group(1)


def parse_tree(tree_html: str, source_id: str) -> list[TocItem]:
    """Страницы книги из фрагмента дерева, в порядке следования."""
    items: list[TocItem] = []
    seen: set[tuple[str, str]] = set()
    for match in _BOOKMARK.finditer(tree_html):
        if match.group("db") != source_id:
            continue
        key = (match.group("chapter"), match.group("page"))
        if key in seen:
            continue
        seen.add(key)
        title = _MULTISPACE.sub(" ", _TAGLESS.sub("", match.group("title"))).strip()
        items.append(TocItem(key[0], key[1], title or key[1]))
    return items


def _read_state(root: Path) -> dict:
    path = root / "_state" / "sync_state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(root: Path, state: dict) -> None:
    path = root / "_state" / "sync_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _segment_at(text: str, boundaries: list[int], pos: int) -> str:
    """Текст страницы от позиции якоря до следующей префиксной границы."""
    nxt = next((b for b in boundaries if b > pos), len(text))
    return text[pos:nxt]


def _boundary_prefixed(anchor_id: str, anchor_name: str) -> bool:
    """Префиксный якорь <Гл>__<Стр> по любому из атрибутов — граница страницы."""
    for cand in (anchor_name, anchor_id):
        if cand and "__" in cand and \
                _PAGE_ANCHOR.match(f'<a name="{cand}">'):
            return True
    return False


def resolve_chapter_files(client: ItsClient, source_id: str,
                          items: list[TocItem], state: dict,
                          log=lambda msg: None) -> dict[str, str]:
    """Имя src-файла для каждой главы: bookmark-страница → iframe src.

    Уже известные главы (в состоянии) сетью не трогаются.
    """
    files: dict[str, str] = dict(state.get("chapter_files") or {})
    first_page: dict[str, str] = {}
    for item in items:
        first_page.setdefault(item.chapter, item.page)
    todo = [ch for ch in first_page if ch not in files]
    log(f"глав в дереве: {len(first_page)}, имя файла неизвестно: {len(todo)}")
    for chapter in todo:
        url = f"{BASE}/db/{source_id}/bookmark/{chapter}/{first_page[chapter]}"
        page = client.get(url)
        match = _IFRAME_SRC.search(page)
        if not match:
            log(f"WARN: у главы {chapter} не найден iframe src — пропуск")
            continue
        files[chapter] = urllib.parse.unquote(
            match.group(1).rsplit("/", 1)[-1].split("#")[0])
    return files


def _write_page(root: Path, chapter: str, page: str, html: str) -> Path:
    path = root / chapter / page / "Ext" / "Help" / "ru.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def corpus_root(source_id: str = "ka25doc", cid: str | None = None) -> Path:
    """Каталог корпуса документации (S1/S3, arch-storage v0.2 §1).

    cid задан — docs-хаб конфигурации: /data/docs/<cid>/<source_id>
    (env COMPASS_DOCS_ROOT для тестов/хоста). Без cid — legacy точка
    host-CLI краулера: WORKSPACE_HUB (хост) или /workspace + Help/<id>;
    перенос старых корпусов в хабы — миграция migrate-to-homes (S4).
    """
    if cid:
        from ..registry import config_docs_root
        return config_docs_root(cid, source_id)
    hub = os.getenv("WORKSPACE_HUB", "")
    base = Path(hub) if hub else Path("/workspace")
    return base / "Help" / source_id


def run_crawl(client: ItsClient, source_id: str = "ka25doc", root: Path | None = None,
              log=lambda msg: None, only_chapters: list[str] | None = None,
              limit_chapters: int | None = None) -> dict:
    """Полный прогон выгрузки; возвращает статистику для статусов.

    root — каталог корпуса (по умолчанию corpus_root); only_chapters —
    выгрузить только перечисленные главы; limit_chapters — первые N глав.
    """
    if root is None:
        root = corpus_root(source_id)
    root.mkdir(parents=True, exist_ok=True)
    state = _read_state(root)
    stats: dict = {"started_at": "", "pages_total": 0, "pages_written": 0,
                   "files_fetched": 0, "files_unchanged": 0, "missing": [],
                   "errors": []}
    from datetime import datetime, timezone
    stats["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    index_page = client.get(f"{BASE}/db/{source_id}")
    tree = client.get(find_toc_url(index_page, source_id))
    items = parse_tree(tree, source_id)
    if only_chapters:
        wanted = set(only_chapters)
        items = [it for it in items if it.chapter in wanted]
    if limit_chapters:
        keep = list(dict.fromkeys(it.chapter for it in items))[:limit_chapters]
        items = [it for it in items if it.chapter in set(keep)]
    stats["pages_total"] = len(items)
    log(f"страниц к выгрузке: {len(items)} (глав: "
        f"{len(set(it.chapter for it in items))})")

    files = resolve_chapter_files(client, source_id, items, state, log)
    state["chapter_files"] = files
    _write_state(root, state)  # краш не должен стоить повторного resolve
    by_chapter: dict[str, list[TocItem]] = {}
    for item in items:
        by_chapter.setdefault(item.chapter, []).append(item)

    written: set[str] = set()
    missing: list[dict] = []
    missing_by_file: dict[str, list[dict]] = {}
    fetched = 0
    for chapter, chapter_items in by_chapter.items():
        filename = files.get(chapter)
        if not filename:
            continue
        raw_path = root / "_src" / filename
        digest = state.get("file_sha", {}).get(filename)
        if digest and raw_path.exists():
            stats["files_unchanged"] += 1
        else:
            url = f"{BASE}/db/content/{source_id}/src/{urllib.parse.quote(filename)}"
            page_html = client.get(url)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(page_html, encoding="utf-8")
            digest = _sha256(page_html.encode("utf-8"))
            state.setdefault("file_sha", {})[filename] = digest
            stats["files_fetched"] += 1
            fetched += 1
            if fetched % 20 == 0:  # прогресс переживает обрыв
                _write_state(root, state)
        segments = dict(split_chapter(raw_path.read_text(encoding="utf-8")))
        for item in chapter_items:
            key = f"{item.chapter}__{item.page}".lower()
            segment = segments.get(key)
            if segment is None:
                missing_by_file.setdefault(filename, []).append(
                    {"chapter": item.chapter, "page": item.page,
                     "title": item.title})
                continue
            path_key = f"{item.chapter}/{item.page}"
            if path_key not in written:
                _write_page(root, item.chapter, item.page,
                            page_document(item.title, segment))
                written.add(path_key)
                stats["pages_written"] += 1

    # ---- голые якоря: часть страниц в файле главы названа без префикса
    # (первый прогон ka25doc: 128 страниц вида <a name="BatchCost">)
    for filename, entries in missing_by_file.items():
        raw_path = root / "_src" / filename
        if not raw_path.exists():
            missing.extend(entries)
            continue
        bare_pages = {entry["page"] for entry in entries}
        segments = dict(split_chapter(raw_path.read_text(encoding="utf-8"),
                                      bare_pages=bare_pages))
        for entry in entries:
            segment = segments.get(entry["page"].lower())
            if segment is None:
                missing.append(entry)
                continue
            path_key = f"{entry['chapter']}/{entry['page']}"
            if path_key not in written:
                _write_page(root, entry["chapter"], entry["page"],
                            page_document(entry["title"], segment))
                written.add(path_key)
                stats["pages_written"] += 1

    # ---- fallback: страницы, чей контент не в файле первой страницы главы
    # или названа якорем иначе (первый прогон ka25doc: 128 из 3354).
    # bookmark-страница страницы даёт файл И фрагмент (#ЯКОРЬ), а корневая
    # страница главы — это голова файла до первой границы.
    if missing:
        log(f"fallback: страниц вне файлов первых страниц глав: {len(missing)}")
        page_files: dict[str, dict] = {}
        for key, value in (state.get("page_files") or {}).items():
            # legacy: строка-имя файла без фрагмента
            page_files[key] = ({"file": value, "fragment": ""}
                               if isinstance(value, str) else value)
        need: dict[str, list[dict]] = {}
        still_missing: list[dict] = []
        for entry in missing:
            key = f"{entry['chapter']}/{entry['page']}"
            info = page_files.get(key)
            if isinstance(info, dict):
                # маппинг уже снят с bookmark-страницы (фрагмент может быть
                # пустым — это финальный ответ, перефетч не нужен)
                need.setdefault(info["file"], []).append(
                    {**entry, "fragment": info.get("fragment", "")})
                continue
            # нет маппинга или legacy-строка без фрагмента
            url = (f"{BASE}/db/{source_id}/bookmark/"
                   f"{entry['chapter']}/{entry['page']}")
            page = client.get(url)
            match = _IFRAME_SRC.search(page)
            if match:
                tail = match.group(1).rsplit("/", 1)[-1]
                fname, _, fragment = tail.partition("#")
                info = {"file": urllib.parse.unquote(fname),
                        "fragment": fragment}
            elif isinstance(info, str):
                info = {"file": info, "fragment": ""}  # апгрейд legacy
            else:
                still_missing.append(entry)
                continue
            page_files[key] = info
            need.setdefault(info["file"], []).append(
                {**entry, "fragment": info.get("fragment", "")})
        state["page_files"] = page_files
        _write_state(root, state)
        for filename, entries in need.items():
            raw_path = root / "_src" / filename
            digest = state.get("file_sha", {}).get(filename)
            if not (digest and raw_path.exists()):
                url = (f"{BASE}/db/content/{source_id}/src/"
                       f"{urllib.parse.quote(filename)}")
                page_html = client.get(url)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(page_html, encoding="utf-8")
                state.setdefault("file_sha", {})[filename] = _sha256(
                    page_html.encode("utf-8"))
                stats["files_fetched"] += 1
                _write_state(root, state)
            text = raw_path.read_text(encoding="utf-8")
            idx = anchor_index(text)
            boundaries = sorted({pos for aid, aname, pos in idx
                                 if _boundary_prefixed(aid, aname)})
            for entry in entries:
                segment = None
                fragment = entry.get("fragment", "").lower()
                if fragment:
                    for aid, aname, pos in idx:
                        # #PRICING4 → Marketing__Pricing4; #RETROBONUS7 → id
                        for cand in (aname, aid):
                            if cand and (cand == fragment
                                         or cand.endswith("__" + fragment)
                                         or cand.endswith(fragment)):
                                segment = _segment_at(text, boundaries, pos)
                                break
                        if segment is not None:
                            break
                if segment is None and entry["page"].lower() == \
                        entry["chapter"].lower():
                    segment = _segment_at(text, boundaries, 0)  # голова файла
                if segment is None:
                    still_missing.append(entry)
                    continue
                _write_page(root, entry["chapter"], entry["page"],
                            page_document(entry["title"], segment))
                stats["pages_written"] += 1
        missing = still_missing

    stats["missing"] = missing
    state["chapter_files"] = files
    state["missing_count"] = len(missing)
    state["last_run"] = stats
    _write_state(root, state)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"готово: страниц {stats['pages_written']}/{stats['pages_total']}, "
        f"файлов {stats['files_fetched']} (без изменений: "
        f"{stats['files_unchanged']}), не найдено якорей: {len(missing)}")
    return stats


def _load_env_its() -> None:
    """Подтянуть ITS_* и WORKSPACE_HUB из .env репозитория (host-прогон CLI:
    docker-compose читает .env только для контейнера). Уже заданные env
    не трогаются.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    wanted = ("ITS_", "WORKSPACE_HUB")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(wanted) and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    """CLI для ручных прогонов (тул/панель вызывают run_crawl из main.py)."""
    parser = argparse.ArgumentParser(
        description="Выгрузка документации ИТС в корпус компаса")
    parser.add_argument("--source-id", default="ka25doc")
    parser.add_argument("--root", default=None,
                        help="каталог корпуса (по умолчанию WORKSPACE_HUB/Help/<id>)")
    parser.add_argument("--limit-chapters", type=int, default=None)
    parser.add_argument("--chapters", nargs="*", default=None)
    parser.add_argument("--delay", type=float, default=None,
                        help="пауза между запросами, с (по умолчанию из env)")
    args = parser.parse_args()
    _load_env_its()

    from ..config import config
    creds = config.its_credentials()
    if creds["source"] == "none":
        print("FAIL: нет кредов (ITS_LOGIN/ITS_PASSWORD или ITS_COOKIE в .env)")
        return 2
    client = ItsClient(login=creds["login"], password=creds["password"],
                       cookie=creds["cookie"],
                       delay=args.delay if args.delay is not None else creds["delay"])
    root = Path(args.root) if args.root else None
    stats = run_crawl(client, args.source_id, root=root, log=print,
                      only_chapters=args.chapters,
                      limit_chapters=args.limit_chapters)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
