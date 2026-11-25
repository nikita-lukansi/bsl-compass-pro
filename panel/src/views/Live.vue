<template>
  <div>
    <!-- ═══ Строка здоровья + ручная загрузка ═══ -->
    <div class="grid">
      <div class="card s8">
        <h3>Конвейер</h3>
        <div class="hrow">
          <span class="dot" :class="health?.writable ? 'live' : 'offline'"></span>
          <span>Каталог обмена<span class="note">{{ health?.root || '—' }}</span></span>
          <span class="val">{{ health ? (health.writable ? 'запись ✓' : 'запись ✗') : '—' }}</span>
        </div>
        <div class="hrow">
          <span class="dot" :class="tasks.length ? 'live' : ''"></span>
          <span>Задач в реестре<span class="note">обновление каждые 5 с</span></span>
          <span class="val">{{ tasks.length }}</span>
        </div>
        <div class="row">
          <button @click="pickFile">⬆ Загрузить ответ вручную</button>
          <input ref="fileInput" type="file" accept=".json"
                 style="display:none" @change="uploadResp">
          <span class="muted">фолбэк без прокинутого диска: resp-файл из 1С</span>
        </div>
      </div>

      <div class="card s4">
        <h3>Статусы</h3>
        <div class="row" style="margin-top:6px">
          <span class="status-badge" style="cursor:pointer"
                :class="!filter ? 'ok' : ''" @click="filter = ''">
            всё · {{ tasks.length - orphans.length }}</span>
          <span v-for="(n, st) in statusCounts" :key="st"
                class="status-badge" style="cursor:pointer"
                :class="filter === st ? badgeClass(st) : ''"
                @click="filter = filter === st ? '' : st"
                :title="'фильтр: ' + statusLabel(st)">
            {{ statusLabel(st) }} · {{ n }}</span>
        </div>
        <div class="ctx">клик по чипу — фильтр таблицы</div>
      </div>
    </div>

    <!-- ═══ Таблица задач ═══ -->
    <h2 class="sect">Задачи</h2>
    <div class="grid">
      <div class="card s12">
        <table class="rows-clickable">
          <thead>
            <tr>
              <th>задача</th><th>чат</th><th>статус</th>
              <th>вызовы</th><th>результат</th><th>действия</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="t in filtered" :key="t.id">
              <tr @click="toggleExpand(t.id)">
                <td>
                  <b class="num">{{ t.id }}</b>
                  <div class="muted" style="font-size:11px">{{ t.description || '—' }}</div>
                </td>
                <td class="muted">{{ t.chat }}</td>
                <td style="white-space:nowrap">
                  <span :class="['status-badge', badgeClass(t.status)]">{{ statusLabel(t.status) }}</span>
                  <span class="muted num"> {{ age(t.created_at) }}</span>
                </td>
                <td class="num">{{ t.calls_methods?.length ?? 0 }}</td>
                <td class="num">{{ resultText(t) }}</td>
                <td @click.stop style="white-space:nowrap">
                  <a v-if="t.status === 'queued' || t.status === 'in_1c'"
                     :href="reqHref(t)" download
                     title="скачать req-файл для 1С">req</a>
                  <button v-if="t.status === 'queued'" class="danger"
                          style="font-size:11px; margin-left:8px"
                          @click="doCancel(t)">отменить</button>
                  <button style="font-size:11px; margin-left:8px"
                          @click="doResubmit(t)">переотправить</button>
                </td>
              </tr>
              <tr v-if="expanded === t.id">
                <td colspan="6" style="padding:10px 14px">
                  <template v-if="cur && cur.id === t.id">
                    <div class="ctx">этапы:
                      <span v-for="(tm, st) in cur.transitions" :key="st"
                            class="num" style="margin-right:12px">
                        {{ statusLabel(st) }}: {{ fmtWhen(tm) }}</span>
                    </div>
                    <div v-if="cur.fatal" class="ctx" style="color:var(--err)">
                      фатально: {{ cur.fatal }}</div>
                    <div v-if="cur.results_note" class="ctx">{{ cur.results_note }}</div>
                    <div v-if="cur.results_source" class="ctx">
                      источник результатов: {{ cur.results_source }}</div>
                    <table v-if="cur.results?.length" style="margin-top:8px">
                      <thead><tr><th>вызов</th><th>ok</th><th>ответ</th></tr></thead>
                      <tbody>
                        <tr v-for="(r, i) in cur.results" :key="i"
                            :style="{ cursor: 'default' }">
                          <td class="num">{{ r.method }}</td>
                          <td :style="r.ok === false ? 'color:var(--err)' : 'color:var(--ok)'">
                            {{ r.ok === false ? '✗' : '✓' }}</td>
                          <td class="muted" style="max-width:560px; overflow-wrap:anywhere">
                            {{ answerPreview(r) }}</td>
                        </tr>
                      </tbody>
                    </table>
                    <div v-else class="ctx">результатов нет</div>
                  </template>
                  <span v-else class="muted">загрузка…</span>
                </td>
              </tr>
            </template>
            <tr v-if="!filtered.length">
              <td colspan="6" class="muted">
                задач нет — агент собирает пакет тулом offline_submit</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ═══ Нераспознанные файлы ═══ -->
    <template v-if="orphans.length">
      <h2 class="sect">Нераспознанные файлы</h2>
      <div class="grid">
        <div class="card s12">
          <div v-for="o in orphans" :key="o.id" class="hrow">
            <span class="dot offline"></span>
            <span><b class="num">{{ o.id }}</b>
              <span class="note">resp без задачи — проверьте task_id</span></span>
            <span class="val">{{ resultText(o) }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { get, post } from '../api/client'

const props = defineProps({ status: Object, refresh: Function })

const tasks = ref([])
const health = ref(null)
const filter = ref('')
const expanded = ref('')
const detail = ref(null)      // owner-вид раскрытой задачи (GET details)
const fileInput = ref(null)
const nowMs = ref(Date.now())
let timer = null

const orphans = computed(() => tasks.value.filter(t => t.unrecognized))
const visible = computed(() => tasks.value.filter(t => !t.unrecognized))
const statusCounts = computed(() => {
  const c = {}
  for (const t of visible.value) c[t.status] = (c[t.status] || 0) + 1
  return c
})
const filtered = computed(() =>
  filter.value ? visible.value.filter(t => t.status === filter.value) : visible.value)

// раскрытая строка: свежий owner-вид с сервера, fallback — строка списка
const cur = computed(() =>
  detail.value || tasks.value.find(t => t.id === expanded.value))

const STATUS_RU = {
  queued: 'ждут 1С', in_1c: 'в 1С', ready: 'готово', delivered: 'выдано',
  error: 'ошибка', expired: 'истекла', cancelled: 'отменена',
}
function statusLabel(st) { return STATUS_RU[st] || st }
function badgeClass(st) {
  if (st === 'ready' || st === 'delivered') return 'ok'
  if (st === 'queued') return 'warn'
  if (st === 'in_1c') return 'fast'
  if (st === 'error' || st === 'expired') return 'err'
  return '' // cancelled — нейтральный
}

// таймер ожидания от created_at (обновляется в цикле опроса)
function age(iso) {
  if (!iso) return ''
  const s = Math.max(0, Math.floor((nowMs.value - new Date(iso).getTime()) / 1000))
  if (s < 60) return `${s} с`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} мин`
  return `${Math.floor(m / 60)} ч ${m % 60} мин`
}

function fmtWhen(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// результат: сколько вызовов вернулось и сколько с ok=false
function resultText(t) {
  const r = t.results
  if (r == null) return t.results_note ? 'утерян' : '—'
  const errs = r.filter(x => x && x.ok === false).length
  return `${r.length} выз.${errs ? ` · ${errs} ошиб.` : ''}`
}

function answerPreview(r) {
  const text = JSON.stringify(r.answer ?? r) || ''
  return text.length > 300 ? text.slice(0, 300) + '…' : text
}

// req-файл: пока задача в очереди/в 1С — файл есть в queue/ (после — архив)
function reqHref(t) {
  return `/api/offline/requests/req_${t.id}${t.status === 'in_1c' ? '.taken' : '.json'}`
}

async function load() {
  nowMs.value = Date.now()
  try {
    const r = await get('/api/offline/tasks')
    tasks.value = r.tasks || []
  } catch { /* вкладка живёт и без ответа сервера */ }
  try {
    health.value = await get('/api/offline/health')
  } catch { health.value = null }
}

async function toggleExpand(id) {
  if (expanded.value === id) {
    expanded.value = ''
    detail.value = null
    return
  }
  expanded.value = id
  detail.value = null
  try {
    detail.value = await get(`/api/offline/tasks/${encodeURIComponent(id)}`)
  } catch { /* раскроем локальными данными строки */ }
}

async function doCancel(t) {
  if (!confirm(`Отменить задачу ${t.id}?`)) return
  const r = await post(`/api/offline/tasks/${encodeURIComponent(t.id)}/cancel`)
  if (r.error) alert(r.error)
  await load()
  props.refresh()
}

async function doResubmit(t) {
  if (!confirm(`Переотправить задачу ${t.id}? Новый task_id, старая задача будет отменена.`)) return
  const r = await post(`/api/offline/tasks/${encodeURIComponent(t.id)}/resubmit`)
  if (r.error) alert(r.error)
  await load()
  props.refresh()
}

function pickFile() {
  fileInput.value?.click()
}

// ручная загрузка resp: файл, который человек принёс из 1С; содержимое —
// сырой JSON ответа (панель выступает «курьером» вместо прокинутого диска)
function uploadResp(e) {
  const file = e.target.files?.[0]
  e.target.value = '' // один и тот же файл можно выбрать повторно
  if (!file) return
  const reader = new FileReader()
  reader.onload = async () => {
    try {
      const data = JSON.parse(reader.result)
      const r = await post('/api/offline/responses', data)
      if (r.error) alert(r.error)
      else await load()
    } catch (err) {
      alert(`Не удалось передать ответ: ${err.message}`)
    }
  }
  reader.onerror = () => alert('Файл не прочитан')
  reader.readAsText(file)
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>
