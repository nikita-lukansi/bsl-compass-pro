// Тесты чистых хелперов Обзора (vitest): запуск — npm run test
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import {
  addSection, appendHistory, barRects, buildView, changedHubs,
  defaultTileLayout, donutSegments, embeddingWarningHost, fmtAgo,
  fmtDuration, hubsAppliedBanner, hubsFromStatus,
  journalDenied, journalRows, JOURNAL_LIMITS, layoutIsDefault,
  loadHistory, loadTileLayout, moveSection, moveTileToSection,
  normalizeTileLayout, removeSection, renameSection, saveHistory,
  saveTileLayout, semPct, speedPerMin, sparkPath, tailLines,
  TILE_SECTIONS, tileLabel, updateView,
} from './helpers'

// Фейковый localStorage: изолирован на каждый тест
function fakeStorage() {
  const map = new Map()
  return {
    getItem: k => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k),
  }
}

const MIN = 60 * 1000

describe('история спарклайна', () => {
  beforeEach(() => { vi.stubGlobal('localStorage', fakeStorage()) })
  afterEach(() => { vi.unstubAllGlobals() })

  it('пустой/битый localStorage — пустая история', () => {
    expect(loadHistory(fakeStorage())).toEqual([])
    const broken = { getItem: () => '{oops', setItem: () => {}, removeItem: () => {} }
    expect(loadHistory(broken)).toEqual([])
  })

  it('appendHistory добавляет точку и сохраняет в storage', () => {
    const storage = fakeStorage()
    let hist = appendHistory([], 10, 0, storage)
    hist = appendHistory(hist, 20, 31 * 1000, storage)
    expect(hist).toEqual([{ t: 0, v: 10 }, { t: 31000, v: 20 }])
    expect(JSON.parse(storage.getItem('compass_sem_hist'))).toHaveLength(2)
  })

  it('appendHistory троттлит: чаще 30 с точка не добавляется', () => {
    let hist = appendHistory([], 10, 0, fakeStorage())
    hist = appendHistory(hist, 15, 10 * 1000, fakeStorage())
    expect(hist).toHaveLength(1)
  })

  it('окно 24 ч: старые точки отбрасываются, потолок 200 точек', () => {
    const now = 25 * 60 * 60 * 1000 // 25 ч от начала отсчёта
    const old = [{ t: 0, v: 1 }]
    const fresh = [{ t: now - 31 * 1000, v: 5 }] // зазор > 30 с — не троттлинг
    const pruned = appendHistory([...old, ...fresh], 6, now, fakeStorage())
    expect(pruned.some(p => p.t === 0)).toBe(false)
    expect(pruned[pruned.length - 1].v).toBe(6)

    const many = Array.from({ length: 300 }, (_, i) => ({ t: i * 1000, v: i }))
    const appended = appendHistory(many, 999, 299000 + 31000, fakeStorage())
    expect(appended).toHaveLength(200)
    expect(appended[appended.length - 1].v).toBe(999)
  })

  it('saveHistory при недоступном storage не падает', () => {
    expect(() => saveHistory([{ t: 1, v: 1 }], null)).not.toThrow()
    const throwing = { getItem: () => null, setItem: () => { throw new Error('full') } }
    expect(() => saveHistory([{ t: 1, v: 1 }], throwing)).not.toThrow()
  })
})

describe('sparkPath', () => {
  it('меньше двух точек — пустая строка', () => {
    expect(sparkPath([])).toBe('')
    expect(sparkPath([{ t: 1, v: 1 }])).toBe('')
  })

  it('нормирует значения в высоту графика', () => {
    const pts = [{ t: 0, v: 0 }, { t: 1000, v: 10 }]
    const path = sparkPath(pts, 100, 40)
    const [x0, y0] = path.split(' ')[0].split(',').map(Number)
    const [x1, y1] = path.split(' ')[1].split(',').map(Number)
    expect(x0).toBe(0)
    expect(x1).toBe(100)
    expect(y0).toBeGreaterThan(y1) // рост значения = линия вверх (y меньше)
  })

  it('плоская линия (min == max) не делит на ноль', () => {
    const pts = [{ t: 0, v: 5 }, { t: 1000, v: 5 }, { t: 2000, v: 5 }]
    expect(sparkPath(pts)).not.toBe('')
    expect(sparkPath(pts).split(' ')).toHaveLength(3)
  })
})

describe('semPct', () => {
  it('обычные доли и потолок 100', () => {
    expect(semPct(250, 1000)).toBe(25)
    expect(semPct(1500, 1000)).toBe(100)
  })
  it('edge: без знаменателя, ноль, отрицательные', () => {
    expect(semPct(100, 0)).toBe(0)
    expect(semPct(100, undefined)).toBe(0)
    expect(semPct(0, 1000)).toBe(0)
    expect(semPct(-5, 1000)).toBe(0)
  })
})

describe('speedPerMin', () => {
  const base = 60 * 60 * 1000 // час
  it('прирост за окно переводится в точки/мин', () => {
    const pts = [{ t: base, v: 100 }, { t: base + 5 * MIN, v: 200 }]
    expect(speedPerMin(pts, 30, base + 6 * MIN)).toBe(20)
  })

  it('edge: мало точек, пустое окно, отрицательный прирост', () => {
    expect(speedPerMin([], 30)).toBe(0)
    expect(speedPerMin([{ t: base, v: 1 }], 30)).toBe(0)
    // обе точки старше окна
    expect(speedPerMin([{ t: 0, v: 1 }, { t: MIN, v: 2 }], 30, base)).toBe(0)
    // прирост отрицательный (пересборка) — 0, а не минус
    const down = [{ t: base, v: 200 }, { t: base + 5 * MIN, v: 100 }]
    expect(speedPerMin(down, 30, base + 6 * MIN)).toBe(0)
  })
})

describe('donutSegments', () => {
  const C = 2 * Math.PI * 42

  it('пустой вход и нулевая сумма → нет сегментов', () => {
    expect(donutSegments([])).toEqual([])
    expect(donutSegments(null)).toEqual([])
    expect(donutSegments([{ v: 0, color: 'a' }])).toEqual([])
  })

  it('длины сегментов в сумме дают окружность', () => {
    const segs = donutSegments([
      { v: 300, color: 'var(--ok)' },
      { v: 100, color: 'var(--warn)' },
    ])
    expect(segs).toHaveLength(2)
    const total = segs.reduce((a, s) => a + parseFloat(s.dasharray), 0)
    expect(total).toBeCloseTo(C, 1)
    // смещения накапливаются отрицательно (SVG сдвигает начало вперёд)
    expect(segs[0].dashoffset).toBe(0)
    expect(segs[1].dashoffset).toBeCloseTo(-parseFloat(segs[0].dasharray), 1)
  })

  it('нулевые части пропускаются, один сегмент — полный круг', () => {
    const segs = donutSegments([
      { v: 0, color: 'a' },
      { v: 50, color: 'b' },
    ])
    expect(segs).toHaveLength(1)
    expect(parseFloat(segs[0].dasharray)).toBeCloseTo(C, 1)
  })
})

describe('barRects', () => {
  it('пустой вход → пустой результат', () => {
    expect(barRects([])).toEqual([])
    expect(barRects(null)).toEqual([])
  })

  it('масштабирует по максимуму: max → h=height-2, ноль → h=0', () => {
    const rects = barRects([0, 10], 100, 80, 0)
    expect(rects[0].h).toBe(0)
    expect(rects[1].h).toBe(78)
    expect(rects[1].y).toBe(2) // прижат к верху
    expect(rects[1].w).toBeCloseTo(100 / 2, 5)
  })

  it('равномерно раскладывает по ширине с зазором', () => {
    const rects = barRects([1, 1, 1], 90, 40, 3)
    expect(rects).toHaveLength(3)
    expect(rects.map(r => r.x)).toEqual([0, 31, 62])
    expect(rects.every(r => r.h === 38)).toBe(true) // все на максимуме
  })

  it('отрицательные значения (мусор) не дают отрицательной высоты', () => {
    const rects = barRects([-5, 3], 100, 40)
    expect(rects[0].h).toBe(0)
    expect(rects[1].h).toBeGreaterThan(0)
  })
})

describe('fmtAgo', () => {
  const now = new Date('2026-11-06T12:00:00Z').getTime()

  it('градации свежести', () => {
    expect(fmtAgo((now - 30 * 1000) / 1000, now)).toBe('только что')
    expect(fmtAgo((now - 5 * MIN) / 1000, now)).toBe('5 мин назад')
    expect(fmtAgo((now - 3 * 60 * MIN) / 1000, now)).toBe('3 ч назад')
  })

  it('старше суток — дата, мусор — тире', () => {
    const twoDaysAgoMs = now - 2 * 24 * 60 * MIN
    const old = fmtAgo(twoDaysAgoMs / 1000, now)
    // ветка даты: тот же локализованный вид, что дал бы toLocaleString,
    // но не «—» и не относительный формат
    expect(old).not.toBe('—')
    expect(old).toBe(new Date(twoDaysAgoMs).toLocaleString('ru-RU',
      { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }))
    expect(old).not.toContain('назад')
    expect(fmtAgo(0, now)).toBe('—')
    expect(fmtAgo(undefined, now)).toBe('—')
  })
})

describe('embeddingWarningHost (SAFE-5, SECURITY.md §4)', () => {
  it('внешний base_url — предупреждение с hostname', () => {
    expect(embeddingWarningHost('https://api.openai.com/v1'))
      .toBe('api.openai.com')
    expect(embeddingWarningHost('http://emb.corp.local:8000/v1'))
      .toBe('emb.corp.local')
  })

  it('локальный vLLM/Ollama — исходящего потока нет, предупреждения нет', () => {
    expect(embeddingWarningHost('http://localhost:8000/v1')).toBe('')
    expect(embeddingWarningHost('http://127.0.0.1:11434/v1')).toBe('')
    expect(embeddingWarningHost('http://127.0.0.2:8000/v1')).toBe('')
    expect(embeddingWarningHost('http://[::1]:8000/v1')).toBe('')
  })

  it('base_url не задан — предупреждения нет', () => {
    expect(embeddingWarningHost('')).toBe('')
    expect(embeddingWarningHost(null)).toBe('')
    expect(embeddingWarningHost(undefined)).toBe('')
    expect(embeddingWarningHost('   ')).toBe('')
  })

  it('строка-хост без схемы — консервативно предупреждение', () => {
    // не парсится как URL: лучше ложная тревога, чем молчание,
    // когда метаданные уходят наружу; bare ::1 — локальный
    expect(embeddingWarningHost('emb.corp.local')).toBe('emb.corp.local')
    expect(embeddingWarningHost('::1')).toBe('')
  })
})

describe('каталоги выгрузок (compass-85s)', () => {
  it('заполнение из /api/status.hubs', () => {
    expect(hubsFromStatus({
      hubs: { dumps_hub: '/dumps', ext_hub: '/ext-dumps',
              docs_hub: '/docs-dumps' },
    })).toEqual({ dumps_hub: '/dumps', ext_hub: '/ext-dumps',
                  docs_hub: '/docs-dumps' })
    // секции нет (старое ядро) — null, панель показывает «недоступно»
    expect(hubsFromStatus({})).toBeNull()
    expect(hubsFromStatus(null)).toBeNull()
    // секция есть, значения не заданы — пустые строки, не null
    expect(hubsFromStatus({ hubs: {} }))
      .toEqual({ dumps_hub: '', ext_hub: '', docs_hub: '' })
  })

  it('payload «Сохранить» — только изменённые и непустые поля', () => {
    const orig = { dumps_hub: '/dumps', ext_hub: '/ext-dumps',
                   docs_hub: '/docs-dumps' }
    expect(changedHubs(orig, {
      dumps_hub: 'D:/Atlas_DumpsConf',
      ext_hub: '/ext-dumps',
      docs_hub: '   ',
    })).toEqual({ dumps_hub: 'D:/Atlas_DumpsConf' })
    expect(changedHubs(orig, orig)).toEqual({})
    // очистка поля не отправляется — env-ключ так не переписывается
    expect(changedHubs(orig, { dumps_hub: '', ext_hub: '', docs_hub: '' }))
      .toEqual({})
    // лишние ключи формы игнорируются
    expect(changedHubs(orig, {
      dumps_hub: '/dumps', ext_hub: '/ext-dumps', docs_hub: '/docs',
      other: 'x',
    })).toEqual({ docs_hub: '/docs' })
  })

  it('баннер применения — из ответа, дефолт force-recreate', () => {
    expect(hubsAppliedBanner({ saved: true, applied: 'после рестарта' }))
      .toBe('docker compose up -d --force-recreate')
    expect(hubsAppliedBanner({
      saved: true, applied: 'docker compose up -d --force-recreate',
    })).toBe('docker compose up -d --force-recreate')
    expect(hubsAppliedBanner({ saved: true, applied: 'restart-panel' }))
      .toBe('restart-panel')
    expect(hubsAppliedBanner({ saved: false })).toBe('')
    expect(hubsAppliedBanner({ error: 'unauthorized' })).toBe('')
    expect(hubsAppliedBanner(null)).toBe('')
  })
})

describe('карточка «Сборка индекса» (compass-cpd)', () => {
  it('fmtDuration: градации секунд/минут/часов, мусор и минус — тире', () => {
    expect(fmtDuration(42)).toBe('42 с')
    expect(fmtDuration(0)).toBe('0 с')
    expect(fmtDuration(7 * 60 + 32)).toBe('7 мин 32 с')
    expect(fmtDuration(7 * 60)).toBe('7 мин')
    expect(fmtDuration(63 * 60)).toBe('1 ч 03 мин')
    expect(fmtDuration(66 * 60)).toBe('1 ч 06 мин')
    expect(fmtDuration(20.4 * 60)).toBe('20 мин 24 с')
    expect(fmtDuration(null)).toBe('—')
    expect(fmtDuration(undefined)).toBe('—')
    expect(fmtDuration('abc')).toBe('—')
    expect(fmtDuration(-5)).toBe('—')
  })

  it('tailLines: последние n непустых строк, не-массив — пусто', () => {
    expect(tailLines(['a', '', 'b', '   ', 'c'], 5)).toEqual(['a', 'b', 'c'])
    expect(tailLines(['a', 'b', 'c', 'd', 'e', 'f'], 3)).toEqual(['d', 'e', 'f'])
    expect(tailLines(null)).toEqual([])
    expect(tailLines('не массив')).toEqual([])
    expect(tailLines([null, undefined, 42], 2)).toEqual(['42'])
    // хвостовые пробелы обрезаются, строки не пустые после trim
    expect(tailLines(['строка  ', ''], 5)).toEqual(['строка'])
  })

  it('buildView: блока нет / не запускалась — idle', () => {
    expect(buildView(null)).toEqual({
      state: 'idle', gen: null, exitCode: null, elapsedSec: null,
      finishedAt: null, tail: [], lastLine: '',
    })
    expect(buildView(undefined).state).toBe('idle')
    expect(buildView('строка').state).toBe('idle')
    // свежий рестарт ядра: BUILD_STATE по умолчанию — всё пусто, gen 0
    expect(buildView({ building: false, started_at: null, started_epoch: null,
      finished_at: null, exit_code: null, pid: null, gen: 0, tail: [] }).state)
      .toBe('idle')
  })

  it('buildView: идёт сборка — elapsed от started_epoch тикает с now', () => {
    const v = buildView({
      building: true, started_epoch: 1000, started_at: '2026-11-20T10:00:00+00:00',
      finished_at: null, exit_code: null, gen: 3, tail: ['строка 1', ''],
    }, 1000 + 7 * 60 + 32)
    expect(v.state).toBe('building')
    expect(v.gen).toBe(3)
    expect(v.elapsedSec).toBe(452)
    expect(v.tail).toEqual(['строка 1', ''])
    expect(v.lastLine).toBe('строка 1')
    // now до старта (часы скакнули) — не уходим в минус
    expect(buildView({ building: true, started_epoch: 1000 }, 900).elapsedSec).toBe(0)
  })

  it('buildView: идёт сборка без started_epoch — fallback на ISO started_at', () => {
    const start = '2026-11-20T10:00:00Z'
    const now = Date.parse(start) / 1000 + 60
    const v = buildView({ building: true, started_epoch: null, started_at: start }, now)
    expect(v.elapsedSec).toBe(60)
    // совсем без времени старта — elapsed null, состояние всё равно building
    const blind = buildView({ building: true }, now)
    expect(blind.state).toBe('building')
    expect(blind.elapsedSec).toBeNull()
  })

  it('buildView: успех — длительность из started/finished, exit 0', () => {
    const v = buildView({
      building: false,
      started_at: '2026-11-20T10:37:11+00:00',
      started_epoch: null,
      finished_at: '2026-11-20T10:40:11+00:00',
      exit_code: 0, gen: 2, tail: ['ok', 'done'],
    })
    expect(v.state).toBe('ok')
    expect(v.exitCode).toBe(0)
    expect(v.elapsedSec).toBe(180)
    expect(v.finishedAt).toBe('2026-11-20T10:40:11+00:00')
    expect(v.lastLine).toBe('done')
  })

  it('buildView: падение (живой кейс watchdog, код −1) — fail + строка watchdog', () => {
    const v = buildView({
      building: false,
      started_at: '2026-11-20T10:37:11+00:00',
      started_epoch: null,
      finished_at: '2026-11-20T10:57:38+00:00',
      exit_code: -1, gen: 1,
      tail: ['watchdog: building=True 20.4 мин, процесс gyrfalcon (pid=165) '
        + 'не жив — принудительное завершение сборки (fc9)'],
    })
    expect(v.state).toBe('fail')
    expect(v.exitCode).toBe(-1)
    expect(v.elapsedSec).toBe(1227)
    expect(v.lastLine).toContain('watchdog')
  })

  it('buildView: edge-кейсы финиша без полного набора полей', () => {
    // exit_code есть, finished_at нет — состояние есть, длительность неизвестна
    const noFinish = buildView({ building: false, exit_code: 1 })
    expect(noFinish.state).toBe('fail')
    expect(noFinish.exitCode).toBe(1)
    expect(noFinish.elapsedSec).toBeNull()
    expect(noFinish.finishedAt).toBeNull()
    // finished_at есть, exit_code нет — консервативно fail с кодом null
    const noExit = buildView({
      building: false, started_at: '2026-11-20T10:00:00Z',
      finished_at: '2026-11-20T10:01:00Z', exit_code: null,
    })
    expect(noExit.state).toBe('fail')
    expect(noExit.exitCode).toBeNull()
    expect(noExit.elapsedSec).toBe(60)
    // мусорный tail не ломает lastLine
    expect(buildView({ building: false, exit_code: 0, tail: 'не массив' }).tail)
      .toEqual([])
  })
})

describe('раскладка Обзора: динамические блоки (dnd/склад)', () => {
  it('normalizeTileLayout: мусор выброшен, дубли схлопнуты, остальное — в склад', () => {
    const norm = normalizeTileLayout({
      sections: [
        { id: 'a', title: 'Мой блок',
          tiles: ['kpi-sessions', 'kpi-unknown', 'kpi-methods', 'kpi-sessions'] },
        { id: 'cfg', tiles: [] },   // дублирующийся с дефолтом id — известные плитки допишутся
        { title: '' },              // без id и заголовка
      ],
      pool: ['dyn-vector', 'dyn-ghost'],
    })
    expect(norm.sections[0])
      .toEqual({ id: 'a', title: 'Мой блок', tiles: ['kpi-sessions', 'kpi-methods'] })
    expect(norm.sections[1].tiles).toEqual(['cfg-active', 'cfg-layers', 'svc-build'])
    expect(norm.sections[2].title).toBe('Блок')
    // инвариант: каждая известная плитка ровно один раз (блоки + склад)
    const all = norm.sections.flatMap(s => s.tiles).concat(norm.pool)
    expect(new Set(all).size).toBe(all.length)
    expect(norm.pool[0]).toBe('dyn-vector') // сохранённый склад первым
    expect(norm.pool).toContain('kpi-calls')
    expect(normalizeTileLayout(null)).toEqual(
      { sections: [], pool: TILE_SECTIONS.flatMap(s => s.tiles) })
  })

  it('moveTileToSection: между блоками, внутри блока, в конец, no-op', () => {
    // из склада → в блок kpi (в конец); склад сначала наполняем удалением блока
    let layout = removeSection(defaultTileLayout(), 'dyn')
    expect(layout.pool).toEqual(['dyn-vector', 'dyn-load'])
    layout = moveTileToSection(layout, 'dyn-vector', 'kpi')
    expect(layout.pool).toEqual(['dyn-load'])
    expect(layout.sections.find(s => s.id === 'kpi').tiles.at(-1)).toBe('dyn-vector')
    // между блоками: cfg-active → в log перед log-calls
    layout = moveTileToSection(layout, 'cfg-active', 'log', 'log-calls')
    expect(layout.sections.find(s => s.id === 'cfg').tiles)
      .toEqual(['cfg-layers', 'svc-build'])
    expect(layout.sections.find(s => s.id === 'log').tiles)
      .toEqual(['log-sessions', 'cfg-active', 'log-calls'])
    // внутри одного блока = смена порядка
    layout = moveTileToSection(layout, 'log-calls', 'log', 'log-sessions')
    expect(layout.sections.find(s => s.id === 'log').tiles)
      .toEqual(['log-calls', 'log-sessions', 'cfg-active'])
    // неизвестная плитка / неизвестный блок — no-op
    expect(moveTileToSection(layout, 'nope', 'kpi')).toBe(layout)
    expect(moveTileToSection(layout, 'kpi-methods', 'nope')).toBe(layout)
  })

  it('блоки: удалить (плитки в склад), добавить (уникальный id), переименовать, сдвинуть', () => {
    let layout = defaultTileLayout()
    // удаление: плитки возвращаются в склад
    layout = removeSection(layout, 'dyn')
    expect(layout.sections.some(s => s.id === 'dyn')).toBe(false)
    expect(layout.pool).toEqual(['dyn-vector', 'dyn-load'])
    expect(removeSection(layout, 'nope')).toBe(layout)
    // добавление: sec-1 занят? нет — свободен; повторное добавление → sec-2
    layout = addSection(layout)
    expect(layout.sections.at(-1)).toEqual({ id: 'sec-1', title: 'Новый блок', tiles: [] })
    layout = addSection(layout)
    expect(layout.sections.at(-1).id).toBe('sec-2')
    // переименование: пустое игнорируется
    layout = renameSection(layout, 'sec-1', '  Моя панель  ')
    expect(layout.sections.at(-2).title).toBe('Моя панель')
    expect(renameSection(layout, 'sec-1', '   ')).toBe(layout)
    // сдвиг: swap соседних (sec-1 встаёт перед svc), за границей no-op
    const ids = () => layout.sections.map(s => s.id)
    layout = moveSection(layout, 'sec-1', -1)
    expect(ids().at(-3)).toBe('sec-1')
    expect(ids().at(-2)).toBe('svc')
    expect(moveSection(layout, 'sec-1', -5)).toBe(layout)
  })

  it('layoutIsDefault и save/load: круговой обход, пусто и битый JSON — дефолт', () => {
    expect(layoutIsDefault(defaultTileLayout())).toBe(true)
    const changed = removeSection(defaultTileLayout(), 'dyn')
    expect(layoutIsDefault(changed)).toBe(false)
    const storage = fakeStorage()
    saveTileLayout(changed, storage)
    expect(loadTileLayout(storage)).toEqual(normalizeTileLayout(changed))
    const broken = { getItem: () => '{oops', setItem: () => {}, removeItem: () => {} }
    expect(loadTileLayout(broken)).toEqual(defaultTileLayout())
    expect(loadTileLayout(fakeStorage())).toEqual(defaultTileLayout())
    // сохранённая «страница без блоков» переживает перезагрузку
    const empty = normalizeTileLayout({ sections: [] })
    saveTileLayout(empty, storage)
    expect(loadTileLayout(storage).sections).toEqual([])
    expect(loadTileLayout(storage).pool).toHaveLength(13)
  })

  it('миграция раскладки первого поколения ({order, hidden})', () => {
    const storage = fakeStorage()
    // старая версия сохраняла полный состав блоков; order мог содержать мусор
    storage.setItem('compass_tiles_overview', JSON.stringify({
      order: {
        cfg: ['cfg-layers', 'cfg-active'],
        kpi: ['kpi-unknown', 'kpi-methods', 'kpi-meta', 'kpi-sessions'],
        dyn: [],
      },
      hidden: ['kpi-calls'],
    }))
    const layout = loadTileLayout(storage)
    // секции дефолтного состава сохранились с их заголовками; svc-build
    // в order не встретился — миграция отправляет его в pool (из входного
    // склада плитки не воскрешаются, это контракт normalizeTileLayout)
    expect(layout.sections.find(s => s.id === 'cfg'))
      .toEqual({ id: 'cfg', title: 'Конфигурация',
                 tiles: ['cfg-layers', 'cfg-active'] })
    expect(layout.sections.find(s => s.id === 'kpi').tiles)
      .toEqual(['kpi-methods', 'kpi-meta', 'kpi-sessions'])
    // скрытые плитки и не размещённые — в складе
    expect(layout.pool).toContain('kpi-calls')
    expect(layout.pool).toContain('dyn-vector')
    expect(layout.pool).toContain('dyn-load')
    // инвариант: каждая плитка ровно один раз
    const all = layout.sections.flatMap(s => s.tiles).concat(layout.pool)
    expect(new Set(all).size).toBe(all.length)
  })

  it('tileLabel: человекочитаемое имя, неизвестный id — как есть', () => {
    expect(tileLabel('kpi-methods')).toBe('Методы')
    expect(tileLabel('nope')).toBe('nope')
  })
})

describe('экран «Журнал» (compass-0x3): разбор GET /api/journal', () => {
  it('journalRows: recent → нормализованные строки, порядок ядра (свежие сверху)', () => {
    const rows = journalRows({
      recent: [
        { ts: '2026-11-23T10:00:00.123+00:00', tool: 'read_function',
          ms: 42.5, ok: true, result_chars: 1200 },
        { ts: '2026-11-23T09:59:00.000+00:00', tool: 'data_query',
          ms: 7, ok: false, args: { query: 'ВЫБРАТЬ 1' } },
      ],
    })
    expect(rows).toEqual([
      { ts: '2026-11-23T10:00:00.123+00:00', tool: 'read_function',
        ms: 42.5, ok: true, result_chars: 1200 },
      // result_chars нет → null («—»); args не переносится в таблицу
      { ts: '2026-11-23T09:59:00.000+00:00', tool: 'data_query',
        ms: 7, ok: false, result_chars: null },
    ])
  })

  it('journalRows: нет recent / не массив — пусто; мусорные записи отфильтрованы', () => {
    expect(journalRows({})).toEqual([])
    expect(journalRows(null)).toEqual([])
    expect(journalRows({ recent: 'не массив' })).toEqual([])
    // не-объекты выбрасываются, уцелевшие записи добираются дефолтами:
    // ts/tool — строки, ms/result_chars — null, ok без флага — не ошибка
    expect(journalRows({ recent: [null, 42, 'x', { tool: 'code_grep' }] }))
      .toEqual([{ ts: '', tool: 'code_grep', ms: null, ok: true,
                  result_chars: null }])
    expect(journalRows({ recent: [{ ms: 'долго', ok: false }] }))
      .toEqual([{ ts: '', tool: '', ms: null, ok: false, result_chars: null }])
  })

  it('journalDenied: 401/403 — журнал за токеном; сеть и прочие HTTP — нет', () => {
    // ошибки get() несут статус в err.status (api/client.js)
    expect(journalDenied(Object.assign(new Error('Нужен админ-токен…'), { status: 401 })))
      .toBe(true)
    expect(journalDenied(Object.assign(new Error('HTTP 403'), { status: 403 }))).toBe(true)
    expect(journalDenied(Object.assign(new Error('HTTP 500'), { status: 500 }))).toBe(false)
    expect(journalDenied(new TypeError('Failed to fetch'))).toBe(false)
    expect(journalDenied(null)).toBe(false)
  })

  it('JOURNAL_LIMITS: селектор 10/50/200 — в границах limit ядра (1..200)', () => {
    expect(JOURNAL_LIMITS).toEqual([10, 50, 200])
  })
})

describe('updateView — разбор /api/status.update (compass-4vt)', () => {
  it('блока нет (старое ядро) / мусор — молча none', () => {
    expect(updateView(undefined)).toEqual({ kind: 'none' })
    expect(updateView(null)).toEqual({ kind: 'none' })
    expect(updateView({})).toEqual({ kind: 'none' })
    expect(updateView('ok')).toEqual({ kind: 'none' })
    // статус вне контракта — тоже молча
    expect(updateView({ status: 'weird', current: 'abc1234' })).toEqual({ kind: 'none' })
  })

  it('ok + устарела: баннер с новой и установленной версиями', () => {
    const v = updateView({
      status: 'ok', current: 'abc1234', latest: 'def5678',
      up_to_date: false, checked_at: '2026-11-20T12:00:00',
    })
    expect(v.kind).toBe('banner')
    expect(v.text).toBe('Доступна новая версия def5678 (установлена abc1234)')
    expect(v.latest).toBe('def5678')
    expect(v.current).toBe('abc1234')
  })

  it('ok + актуальна: тихая строка с текущей версией', () => {
    const v = updateView({
      status: 'ok', current: 'abc1234', latest: 'abc1234',
      up_to_date: true, checked_at: '2026-11-20T12:00:00',
    })
    expect(v).toEqual({ kind: 'quiet', text: 'версия актуальна (abc1234)',
                        current: 'abc1234' })
    // latest при актуальности не важен — строка та же
    expect(updateView({ status: 'ok', current: 'abc1234', up_to_date: true }).text)
      .toBe('версия актуальна (abc1234)')
  })

  it('dev: тихая строка dev-сборки', () => {
    expect(updateView({ status: 'dev', current: 'dev' }))
      .toEqual({ kind: 'quiet', text: 'dev-сборка (dev)', current: 'dev' })
    expect(updateView({ status: 'dev' }).text).toBe('dev-сборка')
  })

  it('unavailable/private: тихая строка одной строкой, detail при наличии', () => {
    const net = updateView({
      status: 'unavailable', current: 'abc1234', up_to_date: null,
      detail: 'таймаут 5 с',
    })
    expect(net.kind).toBe('quiet')
    expect(net.text).toBe('проверка обновлений недоступна: таймаут 5 с')
    // без detail — только префикс, без паники
    expect(updateView({ status: 'unavailable' }).text)
      .toBe('проверка обновлений недоступна')
    const priv = updateView({
      status: 'private', current: 'abc1234',
      detail: 'задайте GITHUB_TOKEN в .env',
    })
    expect(priv.kind).toBe('quiet')
    expect(priv.text).toBe('репозиторий недоступен: задайте GITHUB_TOKEN в .env')
  })

  it('edge: ok без честных полей — не врём ни баннером, ни «актуальна»', () => {
    // ok + up_to_date:false, но latest пуст — баннер без версии не показываем
    expect(updateView({ status: 'ok', current: 'abc1234',
                        up_to_date: false }).kind).toBe('none')
    // ok без булева up_to_date — «актуальна» утверждать нельзя
    expect(updateView({ status: 'ok', current: 'abc1234' }).kind).toBe('none')
    expect(updateView({ status: 'ok', current: 'abc1234',
                        up_to_date: 'yes' }).kind).toBe('none')
  })
})
