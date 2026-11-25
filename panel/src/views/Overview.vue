<template>
  <div>
    <!-- Тулбар раскладки: склад плиток (вне блоков), добавить блок, сброс —
         только в режиме редактирования -->
    <div v-if="editMode" class="row tile-bar">
      <template v-if="layout.pool.length">
        <span class="muted">вне блоков:</span>
        <button v-for="id in layout.pool" :key="id" class="tile-chip"
                :class="{ 'tile-source': dnd.on && dnd.id === id }"
                title="перетащите в блок — или нажмите, чтобы добавить в первый блок"
                @pointerdown="startDrag(id, 'pool', $event)"
                @click="quickAdd(id)">＋ {{ tileLabel(id) }}</button>
      </template>
      <button class="tile-addsec" @click="onAddSection">＋ Добавить блок</button>
      <button v-if="!layoutIsDefault(layout)" class="tile-reset"
              @click="resetLayout">Сбросить раскладку</button>
    </div>

    <!-- Служебный призыв: без конфигурации (не плитка, всегда сверху) -->
    <div v-if="idx?.status === 'not_configured'" class="grid" style="margin-bottom:14px">
      <div class="card s12">
        <h3>Индекс не настроен</h3>
        <div class="ctx" style="font-size:14px">
          Конфигурация не выбрана.
          <router-link to="/settings" style="color:var(--accent2)">
            Перейти к базовым настройкам →
          </router-link>
        </div>
      </div>
    </div>

    <!-- Страница без блоков — легальное состояние -->
    <div v-if="!layout.sections.length" class="card s12" style="margin-bottom:14px">
      <h3>Блоков нет</h3>
      <div class="ctx" style="font-size:14px">
        На странице нет блоков. Включите режим редактирования
        (карандаш в шапке), добавьте блок и перетащите в него плитки
        из склада «вне блоков».
      </div>
    </div>

    <!-- Динамические блоки: заголовок и состав редактируются в режиме
         редактирования; пустые блоки в обычном режиме не показываются -->
    <template v-for="(sec, si) in layout.sections" :key="sec.id">
      <div v-if="editMode || sec.tiles.length" class="section">
      <div class="sect-head">
        <input v-if="editTitle?.id === sec.id" v-model="editTitle.draft"
               v-focus class="sect-input" placeholder="Название блока"
               @keyup.enter="saveTitle(sec.id)" @keyup.esc="cancelTitle"
               @blur="saveTitle(sec.id)">
        <h2 v-else class="sect" title="двойной клик — переименовать"
            @dblclick="editMode && startRename(sec)">{{ sec.title }}</h2>
        <span v-if="editMode" class="sect-tools">
          <button title="Переименовать" @click="startRename(sec)">✎</button>
          <button title="Блок выше" :disabled="si === 0"
                  @click="onMoveSection(sec, -1)">↑</button>
          <button title="Блок ниже"
                  :disabled="si === layout.sections.length - 1"
                  @click="onMoveSection(sec, 1)">↓</button>
          <button class="danger" title="Удалить блок (плитки — в склад)"
                  @click="onRemoveSection(sec)">✕</button>
        </span>
      </div>
      <div class="grid" :data-section="sec.id"
           :class="{ 'tile-drop': dnd.on && dnd.moved && dnd.overSec === sec.id }">
        <div v-for="t in sec.tiles" :key="t" class="card"
             :class="[TILE_SPANS[t], {
               'tile-source': dnd.on && dnd.moved && dnd.id === t,
               'tile-over': dnd.on && dnd.moved && dnd.overTile === t,
             }]"
             :style="{ order: sec.tiles.indexOf(t) + 1 }"
             :data-tile="t">
          <h3 class="tile-head">
            <span v-if="editMode" class="tile-handle" title="Перетащить плитку"
                  @pointerdown="startDrag(t, 'section', $event)">⋮⋮</span>
            {{ tileLabel(t) }}
            <button v-if="editMode" class="tile-hide" title="Убрать плитку в склад"
                    @click="onPoolTile(t)">✕</button>
          </h3>

          <template v-if="t === 'cfg-active'">
            <div v-if="activeCfg" style="margin-bottom:8px">
              <div style="font-size:17px; font-weight:700">{{ cfgName }}</div>
              <div class="ctx">{{ cfgVersion }}</div>
            </div>
            <table v-if="activeCfg">
              <tbody>
                <tr :style="{ cursor: 'default' }">
                  <td class="muted">выгрузка</td><td class="num">{{ activeCfg.source_root }}</td>
                </tr>
                <tr :style="{ cursor: 'default' }">
                  <td class="muted">режим</td>
                  <td><span class="status-badge" :class="activeCfg.mode === 'full' ? 'full' : 'fast'">
                    {{ activeCfg.mode === 'full' ? 'полный' : 'быстрый' }}</span></td>
                </tr>
                <tr :style="{ cursor: 'default' }">
                  <td class="muted">ID</td><td class="num">{{ activeCfg.id }}</td>
                </tr>
                <tr :style="{ cursor: 'default' }">
                  <td class="muted">собрана</td><td>{{ fmtWhen(activeCfg.indexed_at) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="ctx">нет активной конфигурации</div>
          </template>

          <template v-else-if="t === 'cfg-layers'">
            <div class="hrow">
              <span class="dot" :class="sqliteDot"></span>
              <span>SQLite · структурный<span class="note">{{ idx?.counts_note || 'кэш на момент сборки' }}</span></span>
              <span class="val">{{ hasIndex ? `${fmt(counts?.methods)} методов` : '—' }}</span>
            </div>
            <div class="hrow">
              <span class="dot" :class="qdrantDot"></span>
              <span>Qdrant · векторы<span class="note">{{ qdrantNote }}</span></span>
              <span class="val">{{ sem?.configured ? fmt(sem?.points) : '—' }}</span>
            </div>
            <div class="hrow">
              <span class="dot" :class="runtimeDot"></span>
              <span>Рантайм · база 1С<span class="note">{{ runtimeNote }}</span></span>
              <span class="val">{{ rtText }}</span>
            </div>
            <div v-if="rt?.mode === 'offline'" class="hrow">
              <span class="dot live"></span>
              <span>Живые данные · офлайн-конвейер<span class="note">{{ liveLast }}</span></span>
              <span class="val">
                <router-link to="/live" style="color:var(--accent2)">{{ liveCounts }} →</router-link>
              </span>
            </div>
            <div class="hrow">
              <span class="dot" :class="schedule > 0 ? 'live' : ''"></span>
              <span>Авто-сборка<span class="note">{{ queue.length ? `в очереди: ${queue.length}` : 'очередь пуста' }}</span></span>
              <span class="val">{{ schedule ? `каждые ${schedule} мин` : 'выкл' }}</span>
            </div>
          </template>

          <template v-else-if="t === 'kpi-methods'">
            <div class="kpi">{{ hasIndex ? fmt(counts?.methods) : '—' }}</div>
            <div class="ctx">модулей: {{ hasIndex ? fmt(counts?.modules) : '—' }}</div>
          </template>

          <template v-else-if="t === 'kpi-calls'">
            <div class="kpi">{{ hasIndex ? fmt(counts?.calls) : '—' }}</div>
            <div class="ctx">объект→код: {{ hasIndex ? fmt(counts?.metadata_code_usages) : '—' }}</div>
          </template>

          <template v-else-if="t === 'kpi-meta'">
            <div class="kpi">{{ hasIndex ? fmt(counts?.metadata_references) : '—' }}</div>
            <div class="ctx">форм: {{ hasIndex ? fmt(counts?.form_elements) : '—' }} · реквизитов: {{ hasIndex ? fmt(counts?.object_attributes) : '—' }}</div>
          </template>

          <template v-else-if="t === 'kpi-sessions'">
            <div class="kpi" :class="activeSessions ? 'ok' : ''">{{ activeSessions }}</div>
            <div class="ctx">активных чатов (2 мин)</div>
            <div class="ctx">всего привязок: {{ sessions.length }}</div>
          </template>

          <template v-else-if="t === 'dyn-vector'">
            <div class="donut-wrap">
              <svg class="donut" viewBox="0 0 110 110">
                <circle cx="55" cy="55" r="42" fill="none" stroke="var(--surface2)" stroke-width="13"/>
                <circle v-for="(s, i) in donutSegs" :key="i" cx="55" cy="55" r="42"
                        fill="none" :stroke="s.color" stroke-width="13"
                        :stroke-dasharray="s.dasharray" :stroke-dashoffset="s.dashoffset"
                        transform="rotate(-90 55 55)"/>
                <text x="55" y="57" text-anchor="middle" class="donut-num">{{ donutPct }}%</text>
              </svg>
              <div class="donut-side">
                <div class="kpi" :class="sem?.configured ? '' : 'err'">
                  {{ sem?.configured ? fmt(sem?.points) : 'выкл' }}
                </div>
                <div class="ctx">{{ sem?.configured ? (sem?.model || 'модель не задана') : 'EMBEDDING_* не задан' }}</div>
                <div class="ctx">знаменатель — методы (оценка)</div>
              </div>
            </div>
            <div v-if="sem?.indexing" class="eta-line">
              <span v-if="sem?.paused" style="color:var(--warn)"><b>пауза оператора</b></span>
              <template v-else>
                <span>осталось: <b>{{ semEta }}</b></span>
                <span>скорость: <b>{{ fmt(speed) }} т/мин</b></span>
              </template>
            </div>
            <div v-if="sem?.last_stats" class="ctx">
              прошлый прогон: <b class="num">{{ fmt(sem.last_stats.indexed) }}</b> +,
              {{ fmt(sem.last_stats.skipped) }} пропущено<span
                v-if="sem.last_stats.failed_batches">,
                <span style="color:var(--warn)">{{ sem.last_stats.failed_batches }} батчей с ошибками</span></span><span
                v-if="sem.last_stats.finished_at"> · {{ fmtWhen(sem.last_stats.finished_at) }}</span>
            </div>
            <div v-if="sem?.last_stats?.failed_batches > 0 && !sem?.indexing"
                 class="ctx" style="color:var(--warn)">
              часть данных не проиндексирована —
              <button class="btn-ico" @click="doResume">Продолжить</button>
            </div>
            <svg v-if="hist.length >= 2" class="spark" viewBox="0 0 220 40" preserveAspectRatio="none">
              <polyline :points="sparkPts" />
            </svg>
            <div class="row">
              <button v-if="sem?.indexing && sem?.paused" class="btn-ico" @click="doResume">
                <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Продолжить
              </button>
              <button v-else-if="sem?.indexing" class="btn-ico" @click="doPause">
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                Пауза
              </button>
              <button @click="doSemantic"
                      :disabled="!sem?.configured || sem?.indexing || !hasIndex">
                ⚡ Векторизовать
              </button>
            </div>
          </template>

          <template v-else-if="t === 'dyn-load'">
            <svg class="bars" viewBox="0 0 640 80" preserveAspectRatio="none">
              <rect v-for="(b, i) in barSegs" :key="i" :x="b.x" :y="b.y"
                    :width="b.w" :height="b.h" rx="2"
                    :class="hourly[i]?.errors ? 'err' : ''">
                <title>{{ hourly[i]?.hour }} · {{ b.v }} вызовов<span v-if="hourly[i]?.errors"> · {{ hourly[i].errors }} ошибок</span></title>
              </rect>
            </svg>
            <div class="bar-axis">
              <span>{{ hourly[0]?.hour }}</span>
              <span>{{ hourly[12]?.hour }}</span>
              <span>{{ hourly[23]?.hour }} · UTC</span>
            </div>
            <div style="margin-top:10px; font-size:14px">
              <b class="num">{{ fmt(journal?.calls_24h) }}</b> вызовов ·
              <b class="num" :style="journal?.errors_24h ? 'color:var(--err)' : ''">{{ fmt(journal?.errors_24h) }}</b> ошибок ·
              <span class="muted">{{ sem?.configured ? `эмбеддинг: ${sem?.model}` : 'эмбеддинг не настроен' }}</span>
            </div>
          </template>

          <template v-else-if="t === 'log-sessions'">
            <table v-if="sessions.length">
              <thead>
                <tr><th>чат</th><th>конфигурация</th><th>активность</th><th>обновлена</th></tr>
              </thead>
              <tbody>
                <tr v-for="s in sessions" :key="s.chat" :style="{ cursor: 'default' }">
                  <td>{{ s.chat }}</td>
                  <td>{{ s.display_name || s.config_id || '—' }}</td>
                  <td>
                    <span class="dot" :class="sessDot(s)"></span>
                    <span class="num"> {{ fmt(s.requests) }} запр.</span>
                  </td>
                  <td class="num">{{ fmtAgo(s.last_seen) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="ctx">активных сессий пока нет</div>
          </template>

          <template v-else-if="t === 'log-calls'">
            <table v-if="recentCalls.length">
              <thead>
                <tr><th>UTC</th><th>тул</th><th>мс</th><th>статус</th></tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in recentCalls" :key="i" :style="{ cursor: 'default' }">
                  <td class="num">{{ r.ts?.slice(11, 19) }}</td>
                  <td>{{ r.tool }}</td>
                  <td class="num">{{ fmt(r.ms) }}</td>
                  <td :style="r.ok === false ? 'color:var(--err)' : ''">
                    {{ r.ok === false ? '✗ ошибка' : '✓' }}
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="ctx">журнал пуст</div>
          </template>

          <template v-else-if="t === 'svc-build'">
            <div class="ctx" style="font-size:14px; margin-top:0">
              <span class="dot" :class="buildDot" style="display:inline-block; margin-right:6px"></span>
              <b :style="buildTone">{{ buildHeadline }}</b>
              <template v-if="buildCard.state !== 'idle'"> · {{ buildSub }}</template>
            </div>
            <div v-if="buildCard.state === 'fail' && buildCard.lastLine"
                 class="ctx" style="color:var(--err)">
              {{ buildCard.lastLine }}
            </div>
            <pre v-if="buildTail.length" class="build-tail">{{ buildTail.join('\n') }}</pre>
            <div class="ctx">{{ queue.length ? `в очереди: ${queue.join(', ')}` : 'очередь пуста' }} ·
              {{ schedule ? `авто: каждые ${schedule} мин` : 'авто: выкл' }}</div>
            <div class="row">
              <button @click="doRebuild" :disabled="build?.building || !activeCfg">♻ Пересобрать</button>
            </div>
          </template>

          <template v-else-if="t === 'svc-docs'">
            <div class="ctx" style="font-size:14px">
              <template v-if="docs?.fetching">
                <span v-if="docs?.paused" style="color:var(--warn)">⏸ выгрузка — пауза оператора</span>
                <template v-else>⏳ выгрузка корпуса…</template>
              </template>
              <template v-else-if="docsPages">📚 {{ docsSources }}</template>
              <template v-else>корпус не выгружен (help_sync)</template>
            </div>
            <div class="ctx">
              {{ docsStats }} · обновление:
              {{ docs?.configured === 'none' ? 'нет кредов' : docs?.configured }}
            </div>
            <div class="row">
              <button v-if="docs?.fetching && docs?.paused" class="btn-ico" @click="doDocsResume">
                <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Продолжить
              </button>
              <button v-else-if="docs?.fetching" class="btn-ico" @click="doDocsPause">
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                Пауза
              </button>
              <button @click="doDocsSync"
                      :disabled="docs?.fetching || docs?.configured === 'none' || !hasIndex">
                📚 Синхронизировать
              </button>
              <button v-if="docsPages" @click="doDocsRemove" :disabled="docs?.fetching">Отвязать</button>
            </div>
          </template>

          <template v-else-if="t === 'svc-buildlog'">
            <pre style="max-height:140px">{{ buildLog }}</pre>
          </template>
        </div>
        <div v-if="editMode && !sec.tiles.length" class="ctx tile-empty">
          пустой блок — перетащите сюда плитку (или ✕ на плитке другого блока)
        </div>
      </div>
      </div>
    </template>

    <!-- Панель режима редактирования: применить или откатить черновик -->
    <div v-if="editMode" class="edit-bar">
      <span class="muted">режим редактирования</span>
      <button class="primary" @click="applyEdit">Применить</button>
      <button @click="cancelEdit">Отменить</button>
    </div>

    <!-- Призрак перетаскивания: следует за курсором, клики не перехватывает -->
    <div v-if="dnd.on && dnd.moved" class="tile-ghost"
         :style="{ left: dnd.x + 'px', top: dnd.y + 'px' }">
      {{ tileLabel(dnd.id) }}
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { post, fmt } from '../api/client'
import {
  addSection, addTileToSection, appendHistory, barRects, buildView,
  defaultTileLayout, donutSegments, fmtAgo, fmtDuration, layoutIsDefault,
  loadHistory, loadTileLayout, moveSection, moveTileToSection, removeSection,
  removeTileToPool, renameSection, saveTileLayout, semPct, speedPerMin,
  sparkPath, tailLines, TILE_SPANS, tileLabel,
} from '../helpers'

const props = defineProps({
  status: Object,
  refresh: Function,
  editMode: Boolean, // режим редактирования: ручки/скрытие/инструменты блоков
  closeEdit: Function, // Применить/Отменить — выход из режима
})

// -------------------------------------------------- раскладка: блоки и плитки
// Блоки динамические: можно переименовать (✎ / двойной клик), передвинуть
// (↑/↓), удалить (✕ — плитки уходят в склад), добавить. Плитка живёт ровно
// в одном месте: в блоке или в складе «вне блоков». Страница без блоков —
// легальное состояние. Раскладка — в localStorage (compass_tiles_overview),
// записывается только по «Применить».

const saved = ref(loadTileLayout()) // применённая раскладка (обычный режим)
const draft = ref(null) // черновик — существует только в режиме редактирования
// записываемый computed: чтение — черновик поверх применённой раскладки;
// запись маршрутизируется — в режиме редактирования в черновик (localStorage
// не пишется до «Применить»), вне режима — сразу в применённую
const layout = computed({
  get: () => draft.value ?? saved.value,
  set: (v) => { if (props.editMode) draft.value = v; else saved.value = v },
})
watch(saved, l => saveTileLayout(l), { deep: true })

/** «Применить»: черновик становится раскладкой и уходит в localStorage. */
function applyEdit() {
  if (draft.value) saved.value = draft.value
  draft.value = null
  props.closeEdit?.()
}

/** «Отменить»: черновик выбрасывается, раскладка не меняется. */
function cancelEdit() {
  draft.value = null
  props.closeEdit?.()
}

function startEdit() {
  draft.value = JSON.parse(JSON.stringify(saved.value)) // снимок для отмены
}
// вход в режим — снимок; выход любым способом (кнопки, уход со страницы)
// — черновик сбрасывается, если не был применён
watch(() => props.editMode, (on) => {
  if (on) startEdit()
  else draft.value = null
})

const layoutCustom = computed(() => !layoutIsDefault(layout.value))

// перетаскивание — на pointer-событиях (HTML5 DnD капризен: мёртвые зоны
// между карточками, некрасивый системный ghost). Призрак — свой чип,
// цель ищется через document.elementFromPoint: drop работает по всему
// блоку, включая промежутки, и позволяет переносить плитки между блоками.
const dnd = ref({
  on: false, moved: false, id: '', from: '',
  x: 0, y: 0, overSec: '', overTile: '',
})
let suppressClick = false // клик после реального переноса — не трактуем как «добавить»

function startDrag(id, from, event) {
  if (event.pointerType === 'mouse' && event.button !== 0) return
  event.preventDefault() // не выделять текст и не стартовать нативный drag
  dnd.value = {
    on: true, moved: false, id, from,
    x: event.clientX, y: event.clientY, overSec: '', overTile: '',
  }
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', onDragEnd, { once: true })
  window.addEventListener('pointercancel', onDragEnd, { once: true })
}
function onDragMove(event) {
  if (!dnd.value.on) return
  if (!dnd.value.moved &&
      Math.hypot(event.clientX - dnd.value.x, event.clientY - dnd.value.y) < 4) {
    return // порог: клик по ручке не превращается в перенос
  }
  dnd.value.moved = true
  dnd.value.x = event.clientX
  dnd.value.y = event.clientY
  // автопрокрутка у краёв окна — до далёких блоков дотянуться можно
  if (event.clientY < 60) window.scrollBy(0, -14)
  else if (event.clientY > window.innerHeight - 60) window.scrollBy(0, 14)
  const el = document.elementFromPoint(event.clientX, event.clientY)
  const secEl = el?.closest?.('[data-section]')
  const tileEl = el?.closest?.('[data-tile]')
  dnd.value.overSec = secEl?.getAttribute('data-section') || ''
  dnd.value.overTile = tileEl && secEl && tileEl.closest('[data-section]') === secEl
    ? tileEl.getAttribute('data-tile')
    : ''
}
function onDragEnd() {
  window.removeEventListener('pointermove', onDragMove)
  const { id, from, moved, overSec, overTile } = dnd.value
  if (moved && overSec) {
    if (from === 'pool') {
      layout.value = addTileToSection(layout.value, id, overSec)
    } else {
      layout.value = moveTileToSection(layout.value, id, overSec, overTile || null)
    }
  } else if (moved && !overSec && from === 'section') {
    // бросили вне блоков — плитка уходит в склад
    layout.value = removeTileToPool(layout.value, id)
  }
  suppressClick = moved // чип из склада: drag должен «съесть» последующий click
  dnd.value = {
    on: false, moved: false, id: '', from: '',
    x: 0, y: 0, overSec: '', overTile: '',
  }
}

/** Клик по чипу склада: добавить в первый блок (drag обрабатывается выше). */
function quickAdd(id) {
  if (suppressClick) { suppressClick = false; return }
  const first = layout.value.sections[0]
  if (first) layout.value = addTileToSection(layout.value, id, first.id)
}

/** ✕ на плитке — убрать в склад. */
function onPoolTile(id) {
  layout.value = removeTileToPool(layout.value, id)
}

// -------------------------------------------------- блоки: операции

const editTitle = ref(null) // { id, draft } — блок в режиме переименования

function startRename(sec) {
  editTitle.value = { id: sec.id, draft: sec.title }
}
function saveTitle(id) {
  if (editTitle.value?.id !== id) return
  layout.value = renameSection(layout.value, id, editTitle.value.draft)
  editTitle.value = null
}
function cancelTitle() { editTitle.value = null }

function onAddSection() {
  const next = addSection(layout.value)
  layout.value = next
  startRename(next.sections[next.sections.length - 1])
}
function onRemoveSection(sec) {
  layout.value = removeSection(layout.value, sec.id)
}
function onMoveSection(sec, dir) {
  layout.value = moveSection(layout.value, sec.id, dir)
}
function resetLayout() { layout.value = defaultTileLayout() }

// локальная директива: автофокус на input переименования
const vFocus = { mounted: el => el.focus() }

// -------------------------------------------------- данные статуса

const idx = computed(() => props.status?.index)
const counts = computed(() => idx.value?.counts)
const hasIndex = computed(() => idx.value?.status === 'ok')
const build = computed(() => props.status?.build)
const schedule = computed(() => props.status?.schedule_min)
const sem = computed(() => props.status?.semantic)
const docs = computed(() => props.status?.docs)
const rt = computed(() => props.status?.runtime)
const journal = computed(() => props.status?.journal || {})
const sessions = computed(() => props.status?.sessions || [])
const configs = computed(() => props.status?.configs || [])
const queue = computed(() => props.status?.build_queue || [])
const activeCfg = computed(() => configs.value.find(c => c.active) || null)

const recentCalls = computed(() => (journal.value.recent || []).slice(0, 5))
const buildLog = computed(() => (build.value?.tail || []).join('\n') || '—')

// карточка «Сборка индекса»: разбор build-блока + тикающий elapsed.
// Таймер 1 с живёт только пока идёт сборка (опрос статуса — 5 с, elapsed
// между опросами должен двигаться); на выходе со страницы снимается.
const nowSec = ref(Date.now() / 1000)
let buildTicker = null
watch(() => build.value?.building, (on) => {
  clearInterval(buildTicker)
  buildTicker = on
    ? setInterval(() => { nowSec.value = Date.now() / 1000 }, 1000)
    : null
}, { immediate: true })
onUnmounted(() => clearInterval(buildTicker))

const buildCard = computed(() => buildView(build.value, nowSec.value))
const buildTail = computed(() => tailLines(buildCard.value.tail, 5))
const buildDot = computed(() => ({
  building: 'busy', ok: 'live', fail: 'offline', idle: '',
}[buildCard.value.state]))
const buildTone = computed(() => ({
  building: 'color:var(--warn)', ok: 'color:var(--ok)',
  fail: 'color:var(--err)', idle: '',
}[buildCard.value.state]))
const buildHeadline = computed(() => {
  switch (buildCard.value.state) {
    case 'building': return 'идёт сборка'
    case 'ok': return 'успех'
    case 'fail': return `падение (код ${buildCard.value.exitCode ?? '—'})`
    default: return 'не запускалась'
  }
})
// подпись к заголовку: идущей — время от старта, завершённой — длительность
// и момент финиша
const buildSub = computed(() => {
  const c = buildCard.value
  const dur = c.elapsedSec != null ? fmtDuration(c.elapsedSec) : null
  if (c.state === 'building') {
    return dur ? `${dur} от старта` : 'старт неизвестен'
  }
  const when = c.finishedAt ? fmtWhen(c.finishedAt) : null
  return [dur ? `длилась ${dur}` : null, when].filter(Boolean).join(' · ') || '—'
})

// документация ИТС: источники корпуса и последний итог выгрузки
const docsSourcesList = computed(() => docs.value?.sources || [])
const docsPages = computed(() =>
  docsSourcesList.value.reduce((sum, s) => sum + (s.pages || 0), 0))
const docsSources = computed(() =>
  docsSourcesList.value.map(s => `${s.source_id}: ${fmt(s.pages)} стр.`).join(' · '))
const docsStats = computed(() => {
  const ls = docs.value?.last_stats
  if (docs.value?.fetching) return 'идёт выгрузка'
  if (!ls) return 'выгрузок пока не было'
  if (ls.ok === false) return `последний прогон: ошибка (${ls.error || 'подробности в help_status'})`
  return `последний прогон: +${fmt(ls.pages_written)}/${fmt(ls.pages_total)} стр.`
})

// активные сессии: запрос был меньше 2 мин назад
const activeSessions = computed(() => {
  const now = Date.now() / 1000
  return sessions.value.filter(s => s.last_seen && (now - s.last_seen) < 120).length
})

// активная конфигурация: имя и версия/платформа
const cfgName = computed(() => {
  const c = activeCfg.value
  if (!c) return ''
  return c.display_name || c.config_info?.Name || c.id
})
const cfgVersion = computed(() => {
  const info = activeCfg.value?.config_info || {}
  return [info.Version, info.Platform].filter(Boolean).join(' · ') || '—'
})

// ISO-время → «дд.мм чч:мм»
function fmtWhen(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// слои индекса: классы точек
const sqliteDot = computed(() => {
  const st = idx.value?.status
  if (st === 'ok') return 'live'
  if (st === 'building') return 'busy'
  if (st === 'error') return 'offline'
  return ''
})
const qdrantDot = computed(() => {
  if (sem.value?.indexing) return 'busy'
  if (sem.value?.error) return 'offline'
  if (sem.value?.configured && sem.value?.points > 0) return 'live'
  return ''
})
const qdrantNote = computed(() => {
  if (sem.value?.error) return sem.value.error
  return sem.value?.configured ? (sem.value.model || '') : 'не настроен'
})
const runtimeDot = computed(() => {
  const r = rt.value
  if (!r) return ''
  if (r.mode === 'offline') return 'live'
  if (r.configured && r.reachable !== false) return 'live'
  if (r.configured) return 'offline'
  return ''
})
const runtimeNote = computed(() =>
  rt.value?.mode === 'offline' ? 'транспорт-файлы' : (rt.value?.mode || ''))

// офлайн-конвейер (Т2): сводка из status.offline (offline_summary, TASK-G)
const liveCounts = computed(() => {
  const c = props.status?.offline?.counts || {}
  return `ждут ${c.queued || 0} · в 1С ${c.in_1c || 0} · готово ${c.ready || 0}`
})
const liveLast = computed(() => {
  const l = props.status?.offline?.last
  return l ? `${l.description || '(без описания)'} — ${l.status}` : 'задач пока нет'
})

// рантайм: текст значения
const rtText = computed(() => {
  if (!rt.value) return '—'
  if (rt.value.mode === 'offline') return 'офлайн-режим'
  if (rt.value.configured && rt.value.reachable !== false)
    return rt.value['ИмяБазы'] || rt.value.version || 'подключён'
  if (rt.value.configured) return 'обработка не отвечает'
  return 'не настроен'
})

// донат векторизации: векторизовано vs остаток (оценка по методам)
const donutSegs = computed(() => donutSegments([
  { v: sem.value?.points || 0, color: 'var(--ok)' },
  { v: Math.max(0, (counts.value?.methods || 0) - (sem.value?.points || 0)),
    color: 'var(--warn)' },
]))
const donutPct = computed(() =>
  semPct(sem.value?.points || 0, counts.value?.methods))

// гистограмма нагрузки: столбики по часам из journal.hourly
const hourly = computed(() => journal.value.hourly || [])
const barSegs = computed(() =>
  barRects(hourly.value.map(h => h.calls), 640, 80, 3))

// семантика: история выборок для спарклайна (localStorage, окно 24 ч)
const hist = ref(loadHistory())
watch(
  () => [sem.value?.indexing, sem.value?.points],
  ([indexing, points]) => {
    if (indexing && Number.isFinite(points)) {
      hist.value = appendHistory(hist.value, points) // троттлинг 30 с внутри
    }
  },
  { immediate: true },
)
const speed = computed(() => speedPerMin(hist.value))
const semEta = computed(() => {
  const remaining = (counts.value?.methods || 0) - (sem.value?.points || 0)
  if (!speed.value || remaining <= 0) return '—'
  const min = Math.ceil(remaining / speed.value)
  if (min < 60) return `~${min} мин`
  return `~${Math.floor(min / 60)} ч ${min % 60} мин`
})
const sparkPts = computed(() => sparkPath(hist.value, 220, 40))

// сессии: точка активности (live — был запрос меньше 2 мин назад)
function sessDot(s) {
  const fresh = s.last_seen && (Date.now() / 1000 - s.last_seen) < 120
  return fresh ? 'live' : ''
}

async function doRebuild() {
  if (!confirm('Пересобрать индекс активной конфигурации?')) return
  await post('/reindex')
  props.refresh()
}
async function doSemantic() {
  await post('/api/semantic/index')
  props.refresh()
}
async function doPause() {
  await post('/api/semantic/pause')
  props.refresh()
}
async function doResume() {
  await post('/api/semantic/resume')
  props.refresh()
}
async function doDocsSync() {
  await post('/api/docs/sync')
  props.refresh()
}
async function doDocsPause() {
  await post('/api/docs/pause')
  props.refresh()
}
async function doDocsResume() {
  await post('/api/docs/resume')
  props.refresh()
}
async function doDocsRemove() {
  if (!confirm('Отвязать корпус документации от индекса? Файлы останутся на диске; повторная синхронизация поднимет источник заново.')) return
  await post('/api/docs/remove')
  props.refresh()
}
</script>
