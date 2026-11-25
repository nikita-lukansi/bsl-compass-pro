// Чистые хелперы вкладки «Обзор»: история спарклайна семантики,
// скорость векторизации, проценты, «давно». Покрыты helpers.test.js.

const HISTORY_KEY = 'compass_sem_hist' // [{t: epoch мс, v: точек в Qdrant}]
const HISTORY_WINDOW_MS = 24 * 60 * 60 * 1000 // окно 24 ч — как в Community
const HISTORY_MAX = 200 // максимум точек (хватает с запасом при шаге 30 с)
const SAMPLE_MIN_MS = 30 * 1000 // выборка не чаще раза в 30 с

// ------------------------------------------------------------ спарклайн

function pruneHistory(points, now) {
  const cutoff = now - HISTORY_WINDOW_MS
  const fresh = points.filter(p => p && Number.isFinite(p.t) &&
    Number.isFinite(p.v) && p.t >= cutoff)
  return fresh.slice(-HISTORY_MAX)
}

export function loadHistory(storage = globalThis.localStorage) {
  try {
    const raw = storage?.getItem(HISTORY_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? pruneHistory(parsed, Date.now()) : []
  } catch {
    return [] // битый localStorage — начинаем историю заново
  }
}

export function saveHistory(points, storage = globalThis.localStorage) {
  try {
    storage?.setItem(HISTORY_KEY, JSON.stringify(points))
  } catch {
    // localStorage недоступен (private mode) — история только в памяти
  }
}

/** Добавить точку выборки; троттлинг 30 с — возвращает обновлённый массив. */
export function appendHistory(points, value, now = Date.now(),
                              storage = globalThis.localStorage) {
  const last = points[points.length - 1]
  if (last && now - last.t < SAMPLE_MIN_MS) return points
  const next = pruneHistory([...points, { t: now, v: value }], now)
  saveHistory(next, storage)
  return next
}

/** Полилиния SVG для спарклайна: "x1,y1 x2,y2 ..."; пусто при <2 точек. */
export function sparkPath(points, width = 220, height = 40) {
  if (!points || points.length < 2) return ''
  const values = points.map(p => p.v)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1 // плоская линия — растягиваем на середину
  const step = width / (points.length - 1)
  return points
    .map((p, i) => {
      const x = (i * step).toFixed(1)
      const y = (height - 3 - ((p.v - min) / span) * (height - 6)).toFixed(1)
      return `${x},${y}`
    })
    .join(' ')
}

// ------------------------------------------------------- векторизация

/** Процент векторизации: точки / знаменатель-оценка; без знаменателя — 0. */
export function semPct(points, total) {
  if (!total || points <= 0) return 0
  return Math.min(100, Math.round((points / total) * 100))
}

/**
 * Скорость векторизации, точек/мин, по окну windowMin минут истории.
 * Меньше двух точек или отрицательный прирост (пересборка) — 0.
 */
export function speedPerMin(points, windowMin = 30, now = Date.now()) {
  if (!points || points.length < 2) return 0
  const cutoff = now - windowMin * 60 * 1000
  const window = points.filter(p => p.t >= cutoff)
  if (window.length < 2) return 0
  const first = window[0]
  const last = window[window.length - 1]
  const minutes = (last.t - first.t) / 60000
  if (minutes <= 0) return 0
  const delta = last.v - first.v
  return delta > 0 ? Math.round(delta / minutes) : 0
}

// ------------------------------------------------------------- графики

/**
 * Сегменты доната для SVG-кругов: [{dasharray, dashoffset, color}].
 * parts: [{v, color}]; нулевые пропускаются; пустой вход или нулевая
 * сумма → [] (остаётся только фоновый трек). Порт donutSvg из Community
 * (panel_page.py), но возвращает данные, а не строку.
 */
export function donutSegments(parts, radius = 42) {
  if (!parts || !parts.length) return []
  const total = parts.reduce((a, p) => a + (p.v || 0), 0)
  if (total <= 0) return []
  const C = 2 * Math.PI * radius
  let off = 0
  const segs = []
  for (const p of parts) {
    const v = p.v || 0
    if (v <= 0) continue
    const len = (v / total) * C
    segs.push({
      dasharray: `${len.toFixed(2)} ${(C - len).toFixed(2)}`,
      dashoffset: -off || 0, // -0 → 0: для SVG всё равно, для сравнений чище
      color: p.color,
    })
    off += len
  }
  return segs
}

/**
 * Прямоугольники почасовой гистограммы: [{x, y, w, h, v}] для v-for rect.
 * values: неотрицательные числа; масштаб по максимуму; нули → h=0;
 * отрицательные (мусор) → 0.
 */
export function barRects(values, width = 640, height = 80, gap = 3) {
  if (!values || !values.length) return []
  const max = Math.max(...values.map(v => v || 0), 1)
  const w = (width - gap * (values.length - 1)) / values.length
  return values.map((v, i) => {
    const h = Math.max(0, Math.round(((v || 0) / max) * (height - 2)))
    return {
      x: +(i * (w + gap)).toFixed(2),
      y: height - h,
      w: +w.toFixed(2),
      h,
      v: v || 0,
    }
  })
}

// ------------------------------------------------------------ время

/** «давно» для epoch-секунд: только что / N мин назад / N ч назад / дата. */
export function fmtAgo(epochSec, now = Date.now()) {
  if (!epochSec) return '—'
  const ms = epochSec * 1000
  const diff = now - ms
  if (diff < 60 * 1000) return 'только что'
  if (diff < 60 * 60 * 1000) {
    const m = Math.floor(diff / 60000)
    return `${m} мин назад`
  }
  if (diff < 24 * 60 * 60 * 1000) {
    const h = Math.floor(diff / 3600000)
    return `${h} ч назад`
  }
  return new Date(ms).toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// -------------------------------------------- внешний эмбеддинг-API (SAFE-5)

// Локальные хосты — vLLM/Ollama по умолчанию такие: localhost, loopback
// IPv4 (127.0.0.0/8), 0.0.0.0, loopback IPv6 (::1; URL.hostname для IPv6
// возвращает хост в скобках)
const LOCAL_HOST_RE =
  /^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|::1)$/

/** Хост из URL эмбеддинг-API; строка без схемы — как есть. */
export function embeddingHost(url) {
  const raw = String(url || '').trim()
  if (!raw) return ''
  try {
    return new URL(raw).hostname
  } catch {
    return raw
  }
}

/**
 * Условие показа предупреждения SAFE-5 (SECURITY.md §4): хост, на который
 * уходят метаданные методов и тексты запросов, — или '', если исходящего
 * потока нет (base_url пуст либо указывает на локальный vLLM/Ollama).
 * Строка-хост без схемы трактуется консервативно: предупреждение.
 */
export function embeddingWarningHost(url) {
  const host = embeddingHost(url)
  return host && !LOCAL_HOST_RE.test(host) ? host : ''
}

// ------------------------------------- каталоги выгрузок (compass-85s, ТЗ
// techlead: DUMPS_HUB / EXT_HUB / DOCS_HUB из /api/status.hubs → POST
// /api/settings/hubs только изменёнными непустыми полями)

const HUB_KEYS = ['dumps_hub', 'ext_hub', 'docs_hub']

/** Секция hubs из /api/status → {dumps_hub, ext_hub, docs_hub} строками.
 * null — секции нет (старое ядро: панель помечает поля «недоступно,
 * обновите ядро», а не показывает пустые заглушки). */
export function hubsFromStatus(status) {
  const h = status?.hubs
  if (!h || typeof h !== 'object') return null
  const out = {}
  for (const key of HUB_KEYS) out[key] = String(h[key] ?? '')
  return out
}

/**
 * Payload кнопки «Сохранить»: только изменённые И непустые поля каталогов
 * (пустое значение не отправляется — env-ключ так не переписывается).
 * original — env-факты из /api/status, current — поля формы.
 */
export function changedHubs(original, current) {
  const out = {}
  for (const key of HUB_KEYS) {
    const cur = String(current?.[key] ?? '').trim()
    if (cur && cur !== String(original?.[key] ?? '').trim()) out[key] = cur
  }
  return out
}

/**
 * Текст команды применения после сохранения; '' — ответ не успешный.
 * Бэкенд (компас-85s, b1) возвращает applied: 'после рестарта' — тогда
 * показываем фикс-команду; если вернёт команду — берём её как есть.
 */
export function hubsAppliedBanner(response) {
  if (!response || !response.saved) return ''
  const applied = String(response.applied || '').trim()
  return applied && applied !== 'после рестарта'
    ? applied
    : 'docker compose up -d --force-recreate'
}

// ------------------- карточка «Сборка индекса» (compass-cpd): разбор
// /api/status.build (ядро: state.BUILD_STATE) в вью-модель + формат времени

/** ISO-строка бэкенда → epoch-сек; пусто/мусор → null. */
function tsSec(iso) {
  if (typeof iso !== 'string' || !iso) return null
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms / 1000 : null
}

/** Длительность из секунд: «42 с» / «7 мин 32 с» / «1 ч 03 мин»; мусор — «—». */
export function fmtDuration(totalSec) {
  // Number(null) === 0 — null/undefined отсеиваем до приведения
  if (totalSec == null) return '—'
  const s = Math.floor(Number(totalSec))
  if (!Number.isFinite(s) || s < 0) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h) return `${h} ч ${String(m).padStart(2, '0')} мин`
  if (m) return sec ? `${m} мин ${sec} с` : `${m} мин`
  return `${sec} с`
}

/** Последние n строк вывода билдера: пустые выброшены, края обрезаны. */
export function tailLines(tail, n = 5) {
  if (!Array.isArray(tail)) return []
  return tail
    .map(l => String(l ?? '').trimEnd())
    .filter(l => l.trim() !== '')
    .slice(-n)
}

/**
 * Вью-модель карточки «Сборка индекса» из /api/status.build.
 * nowSec — epoch-сек снаружи (тикающий таймер компонента), чтобы elapsed
 * жил между опросами статуса. started_epoch (epoch-сек) ядро держит только
 * во время сборки — поэтому параллельно читается ISO started_at.
 * Состояния: 'building' (идёт) · 'ok' (exit_code 0) · 'fail' (иное, включая
 * watchdog-убийство с кодом −1) · 'idle' (не запускалась / блока нет).
 * Возвращает { state, gen, exitCode, elapsedSec, finishedAt, tail, lastLine }:
 * elapsedSec — для идущей сборки время от старта, для завершённой длительность;
 * lastLine — последняя непустая строка tail (при падении — watchdog).
 */
export function buildView(build, nowSec = Date.now() / 1000) {
  const base = {
    state: 'idle', gen: null, exitCode: null, elapsedSec: null,
    finishedAt: null, tail: [], lastLine: '',
  }
  if (!build || typeof build !== 'object') return base
  const tail = Array.isArray(build.tail) ? build.tail : []
  const gen = Number.isFinite(build.gen) ? build.gen : null
  const lastLine = tailLines(tail, 1)[0] || ''
  const startedSec = Number.isFinite(build.started_epoch)
    ? build.started_epoch
    : tsSec(build.started_at)
  const finishedSec = tsSec(build.finished_at)

  if (build.building) {
    return {
      ...base, state: 'building', gen, tail, lastLine, startedSec,
      elapsedSec: startedSec != null
        ? Math.max(0, Math.round(nowSec - startedSec))
        : null,
    }
  }
  // ни результата, ни отметки финиша — сборка не запускалась (или ядро
  // свежеперезапущено: BUILD_STATE живёт в памяти)
  if (finishedSec == null && build.exit_code == null) {
    return { ...base, gen, tail, lastLine }
  }
  return {
    state: build.exit_code === 0 ? 'ok' : 'fail',
    gen,
    exitCode: Number.isFinite(build.exit_code) ? build.exit_code : null,
    startedSec,
    elapsedSec: startedSec != null && finishedSec != null
      ? Math.max(0, Math.round(finishedSec - startedSec))
      : null,
    finishedAt: typeof build.finished_at === 'string' ? build.finished_at : null,
    tail, lastLine,
  }
}

// ------------------- экран «Журнал» (compass-0x3): разбор ответа
// GET /api/journal (ядро: src/panel_api.py api_journal — {recent: [...]}
// за X-Admin-Token; recent уехал из /api/status в security-волне 2, d8bc9eb)

/** Варианты селектора «показывать» — параметр limit (ядро: 1..200). */
export const JOURNAL_LIMITS = [10, 50, 200]

/**
 * Строки журнала из ответа GET /api/journal: {recent: [запись...]},
 * порядок ядра (свежие сверху) сохраняется. Не-объекты выбрасываются,
 * поля нормализуются до {ts, tool, ms, ok, result_chars}: ts/tool —
 * строки, ms/result_chars — число или null («—» в таблице), отсутствие
 * флага ok — не ошибка (ядро так же трактует в stats_24h). args не
 * переносится: таблица его не показывала и раньше.
 */
export function journalRows(response) {
  const recent = response?.recent
  if (!Array.isArray(recent)) return []
  return recent
    .filter(r => r && typeof r === 'object')
    .map(r => ({
      ts: String(r.ts ?? ''),
      tool: String(r.tool ?? ''),
      ms: Number.isFinite(r.ms) ? r.ms : null,
      ok: r.ok !== false,
      result_chars: Number.isFinite(r.result_chars) ? r.result_chars : null,
    }))
}

/**
 * Ошибка запроса /api/journal — «журнал за админ-токеном»?
 * 401 (нет/неверен X-Admin-Token; на всякий случай и 403) — экран
 * показывает сообщение вместо пустой таблицы; прочее (сеть, 5xx) —
 * тихая деградация: последние успешные строки остаются.
 * Статус приходит в err.status (api/client.js get()).
 */
export function journalDenied(err) {
  return err?.status === 401 || err?.status === 403
}

// ---------------- уведомление об обновлении (compass-4vt): разбор
// /api/status.update (ядро: src/update_check.py) в вью-модель баннера/строки

/**
 * Вью-модель update-блока из /api/status. Контракт ядра:
 * {status: 'ok'|'dev'|'unavailable'|'private', current, latest,
 *  up_to_date, checked_at, detail?}.
 * kind: 'banner' — доступна новая версия (заметный баннер в App.vue);
 * 'quiet' — тихая строка в футере сайдбара; 'none' — молча (блока нет,
 * старое ядро — или мусорные поля, про которые не врём «актуальна»).
 * Возвращает {kind, text, current?, latest?, detail?}.
 */
export function updateView(update) {
  if (!update || typeof update !== 'object') return { kind: 'none' }
  const current = String(update.current ?? '').trim()
  const latest = String(update.latest ?? '').trim()
  const detail = String(update.detail ?? '').trim()
  if (update.status === 'ok') {
    if (update.up_to_date === false && latest) {
      return {
        kind: 'banner',
        text: `Доступна новая версия ${latest} (установлена ${current || '—'})`,
        current, latest,
      }
    }
    if (update.up_to_date === true) {
      return {
        kind: 'quiet',
        text: current ? `версия актуальна (${current})` : 'версия актуальна',
        current,
      }
    }
    return { kind: 'none' } // ok без честного up_to_date/latest — не врём
  }
  if (update.status === 'dev') {
    return {
      kind: 'quiet',
      text: current ? `dev-сборка (${current})` : 'dev-сборка',
      current,
    }
  }
  if (update.status === 'unavailable' || update.status === 'private') {
    const what = update.status === 'private'
      ? 'репозиторий недоступен'
      : 'проверка обновлений недоступна'
    return { kind: 'quiet', text: detail ? `${what}: ${detail}` : what, current, detail }
  }
  return { kind: 'none' }
}

// ------------------------- раскладка «Обзора»: блоки, плитки, склад (dnd)

const TILES_LAYOUT_KEY = 'compass_tiles_overview'

/** Блоки «Обзора» по умолчанию (id, заголовок, состав плиток). */
export const TILE_SECTIONS = [
  { id: 'cfg', title: 'Конфигурация', tiles: ['cfg-active', 'cfg-layers', 'svc-build'] },
  { id: 'kpi', title: 'Показатели', tiles: ['kpi-methods', 'kpi-calls', 'kpi-meta', 'kpi-sessions'] },
  { id: 'dyn', title: 'Динамика', tiles: ['dyn-vector', 'dyn-load'] },
  { id: 'log', title: 'Сессии и журнал', tiles: ['log-sessions', 'log-calls'] },
  { id: 'svc', title: 'Обслуживание', tiles: ['svc-docs', 'svc-buildlog'] },
]

/** Ширина плитки в 12-колоночной сетке (класс s3/s4/s6/s8). */
export const TILE_SPANS = {
  'cfg-active': 's6', 'cfg-layers': 's6',
  'kpi-methods': 's3', 'kpi-calls': 's3', 'kpi-meta': 's3', 'kpi-sessions': 's3',
  'dyn-vector': 's4', 'dyn-load': 's8',
  'log-sessions': 's6', 'log-calls': 's6',
  'svc-build': 's6', 'svc-docs': 's6', 'svc-buildlog': 's6',
}

/** Человекочитаемые имена плиток — склад и ghost при перетаскивании. */
export const TILE_LABELS = {
  'cfg-active': 'Активная конфигурация',
  'cfg-layers': 'Слои индекса',
  'kpi-methods': 'Методы',
  'kpi-calls': 'Граф вызовов',
  'kpi-meta': 'Метаданные',
  'kpi-sessions': 'Сессии',
  'dyn-vector': 'Векторизация',
  'dyn-load': 'Нагрузка · 24 ч',
  'log-sessions': 'Последние сессии',
  'log-calls': 'Последние вызовы',
  'svc-build': 'Сборка индекса',
  'svc-docs': 'Документация ИТС',
  'svc-buildlog': 'Лог сборки',
}

export function tileLabel(id) { return TILE_LABELS[id] || id }

const knownTiles = () => TILE_SECTIONS.flatMap(s => s.tiles)

/** Раскладка по умолчанию. Страница легально существует и без блоков:
 * { sections: [], pool: [все плитки] }. */
export function defaultTileLayout() {
  return {
    sections: TILE_SECTIONS.map(s => ({
      id: s.id, title: s.title, tiles: [...s.tiles],
    })),
    pool: [],
  }
}

/**
 * Нормализация сохранённой раскладки. Инвариант: каждая известная плитка
 * встречается ровно один раз — в каком-то блоке или в складе (pool).
 * Неизвестные id выбрасываются, дубликаты плиток схлопываются (первое
 * вхождение), пустые заголовки → «Блок», дубли id блоков перенумеровываются.
 * После обновления панели новые плитки попадают в склад, раскладка не
 * разваливается.
 */
export function normalizeTileLayout(layout) {
  const known = knownTiles()
  const sections = []
  const placed = new Set()
  const seenIds = new Set()
  const src = Array.isArray(layout?.sections) ? layout.sections : []
  for (const s of src) {
    if (!s || typeof s !== 'object') continue
    let id = typeof s.id === 'string' && s.id ? s.id : `sec-${sections.length + 1}`
    if (seenIds.has(id)) id = `sec-dup-${sections.length + 1}`
    seenIds.add(id)
    const tiles = (Array.isArray(s.tiles) ? s.tiles : [])
      .filter(t => {
        if (!known.includes(t) || placed.has(t)) return false
        placed.add(t) // дедуп внутри блока — сразу, иначе дубли проскочат
        return true
      })
    const title = typeof s.title === 'string' && s.title.trim() ? s.title : 'Блок'
    sections.push({ id, title, tiles })
  }
  // блокам с дефолтными id дописываются их плитки, которых ещё нет нигде —
  // раскладка после обновления панели не теряет новые плитки дефолтных блоков.
  // Плитки из входного склада не воскрешаются: их вынес туда пользователь
  const inputPool = new Set(Array.isArray(layout?.pool) ? layout.pool : [])
  const defaultById = Object.fromEntries(
    TILE_SECTIONS.map(s => [s.id, s.tiles]))
  for (const s of sections) {
    const def = defaultById[s.id]
    if (!def) continue
    for (const t of def) {
      if (!placed.has(t) && !inputPool.has(t)) {
        s.tiles.push(t)
        placed.add(t)
      }
    }
  }
  const storedPool = Array.isArray(layout?.pool) ? layout.pool : []
  const pool = []
  for (const t of [...storedPool, ...known]) {
    if (known.includes(t) && !placed.has(t) && !pool.includes(t)) pool.push(t)
  }
  return { sections, pool }
}

const findSection = (layout, sectionId) =>
  layout.sections.find(s => s.id === sectionId)

/** Переименовать блок; пустой заголовок игнорируется. */
export function renameSection(layout, sectionId, title) {
  const clean = String(title || '').trim()
  if (!clean) return layout
  return {
    ...layout,
    sections: layout.sections.map(s =>
      s.id === sectionId ? { ...s, title: clean } : s),
  }
}

/** Добавить блок в конец со свободным id sec-N (N — минимальный свободный). */
export function addSection(layout, title = 'Новый блок') {
  let n = 1
  while (layout.sections.some(s => s.id === `sec-${n}`)) n += 1
  return {
    ...layout,
    sections: [...layout.sections,
      { id: `sec-${n}`, title: String(title).trim() || 'Блок', tiles: [] }],
  }
}

/** Удалить блок: его плитки возвращаются в склад (не теряются). */
export function removeSection(layout, sectionId) {
  const sec = findSection(layout, sectionId)
  if (!sec) return layout
  return {
    sections: layout.sections.filter(s => s.id !== sectionId),
    pool: [...layout.pool, ...sec.tiles],
  }
}

/** Сдвинуть блок на dir позиций (−1 вверх, +1 вниз); за границей — no-op. */
export function moveSection(layout, sectionId, dir) {
  const i = layout.sections.findIndex(s => s.id === sectionId)
  const j = i + dir
  if (i < 0 || j < 0 || j >= layout.sections.length) return layout
  const sections = [...layout.sections]
  ;[sections[i], sections[j]] = [sections[j], sections[i]]
  return { ...layout, sections }
}

/**
 * Переместить плитку в блок (заодно меняет состав блоков): из своего блока
 * или склада; beforeTile — вставить перед этой плиткой, иначе в конец.
 * Внутри одного блока это и есть смена порядка. Неизвестная плитка — no-op.
 */
export function moveTileToSection(layout, tileId, sectionId, beforeTile = null) {
  if (!knownTiles().includes(tileId)) return layout
  if (!findSection(layout, sectionId)) return layout
  const sections = layout.sections
    .map(s => ({ ...s, tiles: s.tiles.filter(t => t !== tileId) }))
  const pool = layout.pool.filter(t => t !== tileId)
  const dest = sections.find(s => s.id === sectionId)
  const idx = beforeTile ? dest.tiles.indexOf(beforeTile) : -1
  if (idx >= 0) dest.tiles.splice(idx, 0, tileId)
  else dest.tiles.push(tileId)
  return { sections, pool }
}

/** Убрать плитку из блока в склад. */
export function removeTileToPool(layout, tileId) {
  const sections = layout.sections
    .map(s => ({ ...s, tiles: s.tiles.filter(t => t !== tileId) }))
  const pool = layout.pool.includes(tileId) ? layout.pool : [...layout.pool, tileId]
  return { sections, pool }
}

/** Добавить плитку из склада в конец блока. */
export function addTileToSection(layout, tileId, sectionId) {
  return moveTileToSection(layout, tileId, sectionId, null)
}

/** Раскладка отличается от дефолта. */
export function layoutIsDefault(layout) {
  return JSON.stringify(normalizeTileLayout(layout)) ===
    JSON.stringify(defaultTileLayout())
}

/**
 * Миграция раскладки первого поколения ({order: {sec: [ids]}, hidden: []})
 * в новый формат: секции с дефолтными заголовками, скрытые плитки — в склад.
 * Не старый формат → null (загрузка идёт дальше обычной нормализацией).
 */
function migrateLegacyLayout(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  if (Array.isArray(raw.sections) || Array.isArray(raw.pool)) return null
  if (!raw.order || typeof raw.order !== 'object') return null
  const known = knownTiles()
  const sections = []
  const placed = new Set()
  for (const def of TILE_SECTIONS) {
    const stored = Array.isArray(raw.order[def.id]) ? raw.order[def.id] : []
    const tiles = stored.filter(t => known.includes(t) && !placed.has(t))
    tiles.forEach(t => placed.add(t))
    sections.push({ id: def.id, title: def.title, tiles })
  }
  const hidden = Array.isArray(raw.hidden)
    ? raw.hidden.filter(t => known.includes(t) && !placed.has(t))
    : []
  const pool = knownTiles().filter(t => !placed.has(t))
  return normalizeTileLayout({ sections, pool: [...pool, ...hidden] })
}

/** Загрузить раскладку из localStorage; пусто/битый JSON — дефолт. */
export function loadTileLayout(storage = globalThis.localStorage) {
  try {
    const raw = storage?.getItem(TILES_LAYOUT_KEY)
    if (!raw) return defaultTileLayout()
    const parsed = JSON.parse(raw)
    return migrateLegacyLayout(parsed) ?? normalizeTileLayout(parsed)
  } catch {
    return defaultTileLayout()
  }
}

/** Сохранить раскладку в localStorage; недоступное хранилище — молча. */
export function saveTileLayout(layout, storage = globalThis.localStorage) {
  try {
    storage?.setItem(TILES_LAYOUT_KEY, JSON.stringify(layout))
  } catch {
    // localStorage недоступен (private mode) — раскладка только в памяти
  }
}
