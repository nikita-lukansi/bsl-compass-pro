<template>
  <div class="layout">
    <aside class="nav">
      <div class="brand">
        <img src="/logo.jpg" alt="BSL Compass">
        <div class="brand-text">
          <span class="brand-name">BSL Compass</span>
          <span class="sub">Pro · панель</span>
        </div>
      </div>
      <router-link v-for="r in routes" :key="r.name" :to="r.path"
                   class="navitem" active-class="active">
        <span class="ico"><svg viewBox="0 0 24 24" aria-hidden="true"
                               v-html="r.svg"></svg></span>{{ r.label }}
      </router-link>

      <!-- Проект: GitHub + Telegram -->
      <div class="nav-sect">Проект</div>
      <a v-if="links.github" class="navlink" :href="links.github.url"
         target="_blank" rel="noopener" title="Репозиторий на GitHub">
        <span class="ico">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path
            d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
        </span>
        GitHub
        <span v-if="links.github.stars != null" class="stars" title="Звёзды репозитория — поставить можно на github.com">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path
            d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25z"/></svg>{{ fmtStars(links.github.stars) }}
        </span>
      </a>
      <a v-if="links.telegram" class="navlink" :href="links.telegram"
         target="_blank" rel="noopener" title="Telegram автора">
        <span class="ico">
          <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </span>
        Telegram
      </a>

      <div class="foot">
        MCP: /mcp · REST: /health<br>
        обновление: {{ refreshSec }} с<template v-if="updateLine"><br>{{ updateLine }}</template>
      </div>
    </aside>
    <main class="content">
      <div class="wrap">
        <header class="top">
          <h1>{{ currentLabel }}</h1>
          <span class="badge accent">{{ version }}</span>
          <span class="badge">{{ alias }}</span>
          <span class="spacer"></span>
          <span class="state-pill">
            <span class="dot" :class="stateClass"></span>{{ stateText }}
          </span>
          <button v-if="onOverview" class="edit-toggle" :class="{ on: editMode }"
                  :aria-pressed="editMode"
                  :title="editMode ? 'Выйти из режима редактирования' : 'Режим редактирования панели'"
                  @click="editMode = !editMode">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          </button>
          <button @click="toggleTheme" title="Тема">🌗</button>
        </header>
        <div v-if="building" class="banner busy show">🟡 Идёт сборка индекса<template v-if="bannerBuild.elapsedSec != null"> · {{ fmtDuration(bannerBuild.elapsedSec) }}</template><template v-if="bannerBuild.gen != null"> · ген {{ bannerBuild.gen }}</template></div>
        <div v-if="updateInfo.kind === 'banner'" class="banner update show">
          ⬆ {{ updateInfo.text }}
          <button class="banner-btn" :disabled="updateChecking"
                  title="Свежая проверка git-версии; статус обновится сразу после неё"
                  @click="recheckUpdate">{{ updateChecking ? 'проверяю…' : 'проверить снова' }}</button>
        </div>
        <div v-if="offline" class="banner offline show">🔴 Сервер недоступен</div>
        <router-view :status="status" :refresh="refresh"
                     :edit-mode="editMode" :close-edit="closeEdit" />
        <div class="muted" style="margin-top:14px">
          обновлено: {{ updatedAt }}
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { buildView, fmtDuration, updateView } from './helpers'

const route = useRoute()
const status = ref({})
const updatedAt = ref('—')
const links = ref({ github: null, telegram: '' })
const refreshSec = ref(Number(localStorage.getItem('compass_refresh_sec')) || 5)
let timer = null

// Иконки пунктов меню — контурные SVG в стиле Feather (MIT), 24×24:
// заливка none, штрих currentColor — сами перекрашиваются в активной строке
const routes = [
  { path: '/', name: 'overview', label: 'Обзор',
    svg: '<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>' },
  { path: '/configs', name: 'configs', label: 'Конфигурации',
    svg: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>' },
  { path: '/data', name: 'data', label: 'Данные',
    svg: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>' },
  { path: '/live', name: 'live', label: 'Живые данные',
    svg: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>' },
  { path: '/backups', name: 'backups', label: 'Бэкапы',
    svg: '<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><line x1="10" y1="12" x2="14" y2="12"/>' },
  { path: '/journal', name: 'journal', label: 'Журнал',
    svg: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>' },
  { path: '/settings', name: 'settings', label: 'Настройки',
    svg: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>' },
]

const currentLabel = computed(() => {
  const r = routes.find(r => r.path === route.path)
  return r ? r.label : 'Обзор'
})

const version = computed(() => 'v' + (status.value?.version || '—'))
const alias = computed(() => status.value?.alias || '—')
const offline = computed(() => !status.value?.version)

// Последний известный build-блок: при сбое fetch /api/status (ядро занято
// сборкой — живой инцидент 96j) status обнуляется и баннер «идёт сборка»
// пропадал. Пока сервер молчит, кормим computed последним успешным снимком.
const lastBuild = ref(null)
// build-блок для индикаторов: свежий статус, при офлайне — последний известный
const buildInfo = computed(() =>
  status.value?.build || (offline.value ? lastBuild.value : null))
const building = computed(() =>
  buildInfo.value?.building || status.value?.index?.status === 'building')

// Баннер шапки: elapsed тикает раз в секунду между опросами статуса (5 с)
const nowSecTick = ref(Date.now() / 1000)
let buildTicker = null
watch(building, (on) => {
  clearInterval(buildTicker)
  buildTicker = on
    ? setInterval(() => { nowSecTick.value = Date.now() / 1000 }, 1000)
    : null
}, { immediate: true })
onUnmounted(() => clearInterval(buildTicker))
const bannerBuild = computed(() => buildView(buildInfo.value, nowSecTick.value))

// Уведомление об обновлении (compass-4vt): update-блок кормится тем же
// паттерном, что build — при сбое fetch (ядро занято, офлайн) status
// обнуляется, баннер обновления не должен мигать на каждом чихе.
const lastUpdate = ref(null)
const updateBlock = computed(() =>
  status.value?.update || (offline.value ? lastUpdate.value : null))
// kind 'banner' — заметный баннер; при возврате актуальности (следующий
// опрос /api/status) computed пересчитается и баннер скроется сам
const updateInfo = computed(() => updateView(updateBlock.value))
// тихая строка футера: версия/SHA + статус одной строкой, без паники
const updateLine = computed(() =>
  updateInfo.value.kind === 'quiet' ? updateInfo.value.text : '')
const updateChecking = ref(false)

/** «проверить снова» из баннера: свежий запрос git-версии на ядре
 * (GET /api/update-check обновляет кэш), затем обычный опрос статуса.
 * Старое ядро без эндпоинта (404) — молча, баннер не ломаем. */
async function recheckUpdate() {
  if (updateChecking.value) return
  updateChecking.value = true
  try {
    await fetch('/api/update-check')
  } catch {
    // сети нет — статус всё равно перепроверим обычным опросом
  }
  await refresh()
  updateChecking.value = false
}

const stateClass = computed(() => {
  if (offline.value) return 'offline'
  if (building.value) return 'busy'
  return 'live'
})
const stateText = computed(() => {
  if (offline.value) return 'сервер не отвечает'
  if (building.value) return 'идёт сборка'
  return 'все системы в норме'
})

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme')
  const next = cur === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', next)
  localStorage.setItem('compass_theme', next)
}

// Режим редактирования раскладки «Обзора»: ручки/скрытие/инструменты блоков
// видны только пока он включён; черновик применяется или откатывается в
// самом «Обзоре» (кнопки «Применить»/«Отменить» внизу экрана). Уход со
// страницы — выход из режима, несохранённый черновик теряется.
const editMode = ref(false)
const onOverview = computed(() => route.path === '/')
watch(() => route.path, () => { editMode.value = false })
function closeEdit() { editMode.value = false }

async function refresh() {
  try {
    const r = await fetch('/api/status')
    const data = await r.json()
    status.value = data
    if (data?.build) lastBuild.value = data.build
    if (data?.update) lastUpdate.value = data.update
    updatedAt.value = new Date().toLocaleTimeString('ru-RU')
  } catch (e) {
    status.value = {}
  }
}

// ссылки проекта — один раз при загрузке (счётчик звёзд кэшируется на бэкенде)
async function loadLinks() {
  try {
    const r = await fetch('/api/links')
    if (r.ok) links.value = await r.json()
  } catch {
    // сайдбар живёт и без ссылок
  }
}

function fmtStars(n) {
  return Number(n || 0).toLocaleString('ru-RU')
}

// период опроса /api/status: настройка из Настроек (localStorage),
// применяется на лету — вкладка диспетчит compass-refresh-changed
function applyRefreshTimer() {
  clearInterval(timer)
  timer = setInterval(refresh, refreshSec.value * 1000)
}

onMounted(() => {
  const theme = localStorage.getItem('compass_theme') || 'dark'
  document.documentElement.setAttribute('data-theme', theme)
  loadLinks()
  refresh()
  applyRefreshTimer()
  window.addEventListener('compass-refresh-changed', (e) => {
    refreshSec.value = Number(e.detail) || refreshSec.value
    applyRefreshTimer()
  })
})
onUnmounted(() => clearInterval(timer))
</script>
