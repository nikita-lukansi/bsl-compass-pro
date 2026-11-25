#!/usr/bin/env bash
# export_docs.sh — выгрузка корпуса документации ИТС из контейнера на хост.
#
# Назначение: снимок корпуса справки конфигурации в DOCS_HUB-совместимый
# каталог на хосте — перенос между машинами/контурами и приём другим контуром
# через help_import без ИТС-кредов (машина с ИТС-доступом выгрузила — остальные
# импортировали).
#
# Синтаксис (запуск С ХОСТА, из корня репо, Git Bash):
#   scripts/export_docs.sh [--force] [cid] [dest]
#   scripts/export_docs.sh [--force] --all [dest_dir]
#
#   cid      — ID конфигурации (Compass_<alias>_<uid>); без cid — активная
#              конфигурация из http://127.0.0.1:8240/api/status;
#   dest     — каталог назначения; по умолчанию ./docs-dumps/<source_id> —
#              совместимо с хабом DOCS_HUB (help_import подхватывает как есть);
#   --all    — все конфигурации, у которых есть корпус в /data/docs/
#              (каждый source_id — в <dest_dir>/<source_id>);
#   --force  — разрешить перезапись существующего dest (без него — отказ).
#
# Примеры:
#   scripts/export_docs.sh                                   # активная конфигурация
#   scripts/export_docs.sh Compass_KA_798247c2               # конкретная
#   scripts/export_docs.sh Compass_KA_798247c2 /d/tmp/kadoc  # в свой каталог
#   scripts/export_docs.sh --all ../backups/docs             # все корпуса
#
# Границы:
#   - хостовой скрипт (в отличие от reindex.py — тот ВНУТРИ контейнера);
#     контейнер bsl-compass-pro должен существовать и быть запущен;
#   - корпус только читается (docker exec ls + docker cp), ничего не мутирует;
#   - для узнавания активной конфигурации нужен python в PATH (jq не требуется).
set -euo pipefail

# Git Bash конвертирует POSIX-пути в аргументах (/data/... ->
# C:/Program Files/Git/data/...) и ломает docker exec. Отключаем конверсию
# ТОЧЕЧНО — только для docker exec (контейнерные пути); docker cp нуждается
# в конверсии хостовой стороны (POSIX-путь назначения -> Windows-путь).


CONTAINER=bsl-compass-pro
API_STATUS_URL=http://127.0.0.1:8240/api/status
DOCS_ROOT=/data/docs
DEFAULT_HUB=./docs-dumps

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "ОТКАЗ: $*" >&2; exit 1; }

# Список записей каталога ВНУТРИ контейнера (пусто, если каталога нет).
# MSYS_NO_PATHCONV=1 — только на эту команду: контейнерный путь /data/...
# без этого Git Bash подменяет на C:/Program Files/Git/data/...
ctr_ls() {
  MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" ls -1 "$1" 2>/dev/null || true
}

# Непустые строки ctr_ls; статус всегда 0 (grep . на пустом вводе падает,
# а с set -e/pipefail это молча убивало скрипт до сообщения об отказе).
ctr_list() { ctr_ls "$1" | grep . || true; }

# Активный cid из статуса API (пусто при любой ошибке: API лежит / нет python).
active_cid() {
  curl -sf "$API_STATUS_URL" | python -c \
    'import json,sys; print(next((c["id"] for c in json.load(sys.stdin).get("configs", []) if c.get("active")), ""))' \
    2>/dev/null || true
}

# Выгрузка одного корпуса: $1=cid, $2=source_id, $3=dest (его создадим).
# «/.» в источнике docker cp — копирование СОДЕРЖИМОГО source_id в dest.
export_one() {
  local cid="$1" sid="$2" dest="$3" files size
  if [[ -e "$dest" ]]; then
    [[ $FORCE -eq 1 ]] || die "dest уже существует: $dest (перезапись — --force)"
    rm -rf -- "$dest"
  fi
  mkdir -p -- "$dest"
  docker cp "$CONTAINER:$DOCS_ROOT/$cid/$sid/." "$dest"
  files=$(($(find "$dest" -type f | wc -l)))
  size=$(du -sh "$dest" | cut -f1)
  echo "  $cid/$sid -> $dest : файлов $files, размер $size"
}

FORCE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --all) ARGS+=("$a") ;;  # режим-флаг: дождётся позиционного разбора ниже
    -h|--help) usage; exit 0 ;;
    -*) die "неизвестный флаг: $a (см. scripts/export_docs.sh --help)" ;;
    *) ARGS+=("$a") ;;
  esac
done
set -- "${ARGS[@]}"

ALL=0
CID=
DEST=
if [[ ${1:-} == "--all" ]]; then
  ALL=1; shift; DEST_BASE="${1:-}"
  [[ $# -le 1 ]] || die "лишние аргументы после dest_dir: $*"
else
  CID="${1:-}"; DEST="${2:-}"
  [[ $# -le 2 ]] || die "лишние аргументы: $* (cid, затем dest)"
fi

# cid не задан и режим не --all — берём активную конфигурацию из API.
if [[ -z "$CID" && $ALL -eq 0 ]]; then
  CID=$(active_cid)
  if [[ -z "$CID" ]]; then
    avail=$(ctr_list "$DOCS_ROOT" | tr '\n' ' '); avail="${avail:-<ни у кого>}"
    die "не удалось узнать активную конфигурацию ($API_STATUS_URL или python \
недоступны?) — укажите cid явно; корпуса есть у: $avail"
  fi
fi

state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null) \
  || die "контейнер $CONTAINER не найден — это хостовой скрипт; поднимите контур \
(docker compose up -d из корня репо)"
[[ $state == "running" ]] || die "контейнер $CONTAINER не запущен (статус: $state)"

ok=0
if [[ $ALL -eq 1 ]]; then
  cids=$(ctr_list "$DOCS_ROOT")
  [[ -n "$cids" ]] || die "в $DOCS_ROOT нет ни одного корпуса справки (help_sync?)"
  for cid in $cids; do
    for sid in $(ctr_list "$DOCS_ROOT/$cid"); do
      export_one "$cid" "$sid" "${DEST_BASE:-$DEFAULT_HUB}/$sid"
      ok=$((ok + 1))
    done
  done
else
  sids=$(ctr_list "$DOCS_ROOT/$CID")
  if [[ -z "$sids" ]]; then
    avail=$(ctr_list "$DOCS_ROOT" | tr '\n' ' '); avail="${avail:-<ни у кого>}"
    die "у $CID нет корпуса справки в $DOCS_ROOT (help_sync не выполнялся?); \
корпуса есть у: $avail"
  fi
  total=$(echo "$sids" | wc -l)
  for sid in $sids; do
    # Явный dest при единственном корпусе — сам каталог назначения; иначе
    # (несколько source_id) — база, внутри которой <dest>/<source_id>.
    if [[ -n "$DEST" && $total -eq 1 ]]; then
      dest="$DEST"
    else
      dest="${DEST:-$DEFAULT_HUB}/$sid"
    fi
    export_one "$CID" "$sid" "$dest"
    ok=$((ok + 1))
  done
fi

echo "Готово: выгружено корпусов — $ok"
