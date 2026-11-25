# NOTICE — атрибуция сторонних компонентов

Кому принадлежат чужие куски внутри образа и под какими лицензиями.
Читай при аудите лицензий или перед добавлением нового vendored-компонента.

Copyright 2026 Nikita Lukansi (n.lukansi)

При включении каждого компонента фиксируется здесь: источник, версия
(vendored-снапшот), лицензия, что взято, что изменено.

## gyrfalcon — строитель индекса (блок разведки)

| Поле | Значение |
|---|---|
| Источник | github.com/aleksandrgradoboev-svg/gyrfalcon |
| Версия (заморожена) | **v0.1.20** (2026-10-26) |
| Лицензия | MIT — текст: `vendored/gyrfalcon/LICENSE`, копия: `licenses/gyrfalcon-LICENSE` |
| Что взято | полный исходник (crates: gyrfalcon-index, gyrfalcon-mcp, gyrfalcon-parser) |
| Что изменено | **ничего в коде** (pristine fork); удалён только `data/` (41 МБ тестовых фикстур, не участвует в сборке — проверено по Cargo-манифестам) |
| Как используем | CLI `gyrfalcon build` как подпроцесс; их MCP-сервер не запускается; контракт — схема SQLite `index.db` |
| Обновление | осознанный шаг: заменить каталог целиком, сверить CHANGELOG и схему, прогнать наши тесты фасада |

Корпус стандартов ИТС (one_c_standards.json) исключён из поставляемого
бинарника патчем (замена include_str! на пустую заглушку в
practice_mining.rs и project_practices.rs — второе исключение pristine,
решение владельца 2026-11-24); туры майнинга практик не активны.

## vue + vue-router — фронтенд панели

| Поле | Значение |
|---|---|
| Источник | npm: `vue`, `vue-router` (panel/package.json) |
| Версии | vue **3.5.44**, vue-router **4.6.4** (фиксация package-lock.json) |
| Лицензия | MIT — тексты: `panel/node_modules/vue/LICENSE` (© 2018-present Yuxi (Evan) You), `panel/node_modules/vue-router/LICENSE` (© 2019-present Eduardo San Martin Morote) |
| Что взято | скомпилированы в собранный фронтенд панели (vite build, минификация) |
| Что изменено | ничего; исходники библиотек не модифицируются |
| Копии в образе | `/app/licenses/vue-LICENSE`, `/app/licenses/vue-router-LICENSE` (стейдж panel Dockerfile) |

## tree-sitter-bsl — грамматика BSL в бинарнике gyrfalcon

| Поле | Значение |
|---|---|
| Источник | vendored-снапшот gyrfalcon: `crates/gyrfalcon-parser/vendor/tree-sitter-bsl` (upstream подтягивает грамматику как vendor-зависимость сборки) |
| Лицензия | MIT — © 2025 Koryakin Aleksey <alkoleft@gmail.com>; текст: `vendored/gyrfalcon/crates/gyrfalcon-parser/vendor/tree-sitter-bsl/LICENSE` |
| Что взято | грамматика скомпилирована в бинарник `gyrfalcon` (входит в образ как есть, вместе со снапшотом gyrfalcon) |
| Что изменено | ничего (pristine — см. секцию gyrfalcon) |
| Копия в образе | `/app/licenses/tree-sitter-bsl-LICENSE` |

Отдельная SDBL-грамматика в vendor не поставляется (в дереве только
tree-sitter-bsl — проверено поиском по `vendored/gyrfalcon/**/vendor`).

Копия лицензии проекта (AGPL-3.0, `LICENSE.md`) и настоящий NOTICE
кладутся в образ как `/app/licenses/LICENSE.md` и `/app/licenses/NOTICE.md`.

## Кандидаты (не включались)

- code-index-mcp (MIT) — отбракован спайком (медленнее 2×, демон-налог)
- MCP-DB-Client http1c (MIT) — не потребовался
- mcp-1c-v1 (MIT, мёртв с 2025-08), 1c-mcp-toolkit (GPL-3.0 — не включался;
  2026-11-12: сверка нашей обработки «КомпасРантайм» с их кодом — чистая
  реализация, 0 пересечений имён/строк/литералов),
  1c-trusted-gateway (лицензии НЕТ — код не смотреть и не копировать)
