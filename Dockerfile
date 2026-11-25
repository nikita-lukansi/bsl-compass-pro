# syntax=docker/dockerfile:1
# BSL Compass Pro — многостейджевая сборка (паттерн gyrfalcon: кэш cargo
# в mount-кэше докера, slim-рантайм):
#   stage rust    — компиляция vendored-строителя индекса (pristine, MIT)
#   stage panel   — сборка Vue-панели (node:22-alpine, R9/A13)
#   stage runtime — наш фасад + семантический слой (ОДИН рантайм-стейдж)
#
# ── Зеркала базовых образов в GHCR (compass-7su; день X = dx4.6) ────────────
# Страховка от морганий auth.docker.io: workflow mirror-images.yml публикует
# ghcr.io/nikita-lukansi/bsl-compass-pro/base/<имя>:<тег исходника>
# (+ датированные <тег>-ГГГГММДД для отката). Механика проверена: ARG до
# первого FROM применяется в строках FROM (BuildKit, syntax-директива выше).
# ПОКА НЕ ВКЛЮЧЕНО — дефолт docker.io, поведение сборки не меняется.
# В день X: раскомментировать ARG, заменить три FROM на варианты с
# ${BASE_REGISTRY}/ и собирать (проброс build.args уже есть в
# docker-compose.yml):
#   BASE_REGISTRY=ghcr.io/nikita-lukansi/bsl-compass-pro/base \
#     GIT_SHA=$(git rev-parse --short HEAD) docker compose build
# ARG BASE_REGISTRY=docker.io
# FROM ${BASE_REGISTRY}/rust:1-bookworm AS rust
# FROM ${BASE_REGISTRY}/node:22-alpine AS panel
# FROM ${BASE_REGISTRY}/python:3.12-slim AS runtime
FROM rust:1-bookworm AS rust

WORKDIR /build
COPY vendored/gyrfalcon/ .

# rusqlite собирает SQLite из исходников, грамматики BSL/SDBL — своим
# build.rs: обоим нужен cc, которого в slim нет (полный bookworm).
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release -p gyrfalcon-mcp \
 && mkdir -p /out \
 && cp target/release/gyrfalcon /out/gyrfalcon

# Панель собирается в образе — локальный npm ci не обязателен
FROM node:22-alpine AS panel

WORKDIR /panel
COPY panel/package.json panel/package-lock.json ./
RUN npm ci
COPY panel/ ./
RUN npm run build
# Лицензии фронтенд-зависимостей (vue, vue-router — MIT): обязанность
# уведомления при распространении собранной панели
RUN mkdir -p /out-licenses \
 && cp node_modules/vue/LICENSE /out-licenses/vue-LICENSE \
 && cp node_modules/vue-router/LICENSE /out-licenses/vue-router-LICENSE

FROM python:3.12-slim AS runtime

WORKDIR /app

# Зависимости отдельным слоем — кэш не падает от правок кода
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Наш код + собранная панель (Vue 3 SPA — из node-стейджа)
COPY src/ ./src/
# Ручные операции (compass-6z4): reindex.py — переиндексация/добавление
# конфигураций в обход панели (docker exec ... python /app/scripts/reindex.py)
COPY scripts/ ./scripts/
COPY --from=panel /panel/dist/ ./panel/dist/

# Rust-строитель индекса из первого стейджа
COPY --from=rust /out/gyrfalcon /usr/local/bin/gyrfalcon

# Лицензии сторонних компонентов (обязанность MIT — уведомление в копиях)
COPY vendored/gyrfalcon/LICENSE /app/licenses/gyrfalcon-LICENSE
# Грамматика tree-sitter-bsl (MIT) — скомпилирована в бинарник gyrfalcon
COPY vendored/gyrfalcon/crates/gyrfalcon-parser/vendor/tree-sitter-bsl/LICENSE \
     /app/licenses/tree-sitter-bsl-LICENSE
# Лицензии фронтенд-зависимостей (vue, vue-router — MIT) из panel-стейджа
COPY --from=panel /out-licenses/ /app/licenses/
# Лицензия проекта (AGPL — копия обязательна при передаче) и NOTICE
COPY LICENSE.md /app/licenses/LICENSE.md
COPY NOTICE.md /app/licenses/NOTICE.md

# git SHA образа (1mj.7): bake при сборке — видимое отличие «старого
# контейнера» в /health и /api/status; из команды:
#   GIT_SHA=$(git rev-parse --short HEAD) docker compose build
ARG GIT_SHA=dev

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8240 \
    COMPASS_GIT_SHA=${GIT_SHA}

EXPOSE 8240

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8240/health', timeout=3)" || exit 1

CMD ["python", "-m", "src.main"]
