<template>
  <div class="grid">
    <!-- Обновление панели -->
    <div class="card s6">
      <h3>Обновление панели</h3>
      <div class="ctx" style="margin-bottom:10px">
        Как часто панель опрашивает состояние сервера. Чаще — живее
        показатели, но и больше трафика; для фоновой работы достаточно 15–30 с.
      </div>
      <div class="row" style="margin-top:0">
        <select v-model.number="refreshSec" style="min-width:140px">
          <option :value="5">5 секунд</option>
          <option :value="10">10 секунд</option>
          <option :value="15">15 секунд</option>
          <option :value="30">30 секунд</option>
          <option :value="60">1 минута</option>
          <option :value="120">2 минуты</option>
          <option :value="300">5 минут</option>
        </select>
        <span v-if="savedNote" class="ctx" style="color:var(--ok)">✅ {{ savedNote }}</span>
      </div>
    </div>

    <div class="card s6">
      <h3>Система</h3>
      <table>
        <tbody>
          <tr v-for="row in rows" :key="row[0]" :style="{ cursor: 'default' }">
            <td class="muted">{{ row[0] }}</td>
            <td>{{ row[1] }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card s6">
      <h3>Подсказки</h3>
      <div class="muted">
        Админ-токен: в консоли браузера<br>
        <code>localStorage.setItem("compass_admin_token", "...")</code>
      </div>
    </div>

    <!-- Каталоги выгрузок (compass-85s): DUMPS_HUB / EXT_HUB / DOCS_HUB -->
    <div class="card s12">
      <h3>Каталоги выгрузок</h3>
      <div class="ctx" style="margin-bottom:10px">
        Папки-источники выгрузок на хосте (маунты контейнера). Сохранение
        переписывает ключи в .env; чтобы применилось, контейнер пересоздают.
      </div>
      <template v-if="hubsAvail">
        <div class="emb-grid">
          <label>Дампы конфигураций (DUMPS_HUB)
            <input v-model="hubsForm.dumps_hub" type="text"
                   placeholder="/dumps">
          </label>
          <label>Дампы расширений (EXT_HUB)
            <input v-model="hubsForm.ext_hub" type="text"
                   placeholder="/ext-dumps">
          </label>
          <label>Корпус документации (DOCS_HUB)
            <input v-model="hubsForm.docs_hub" type="text"
                   placeholder="/docs-dumps">
          </label>
        </div>
        <div class="row" style="justify-content:flex-end">
          <button class="primary" @click="saveHubs"
                  :disabled="hubsBusy || !hubsChangedCount">
            {{ hubsBusy ? '⏳ Сохраняю…' : 'Сохранить' }}
          </button>
          <span v-if="!hubsChangedCount && !hubsBusy" class="muted">
            изменений нет</span>
        </div>
      </template>
      <template v-else>
        <div class="emb-grid">
          <label>Дампы конфигураций (DUMPS_HUB)
            <input type="text" disabled placeholder="недоступно">
          </label>
          <label>Дампы расширений (EXT_HUB)
            <input type="text" disabled placeholder="недоступно">
          </label>
          <label>Корпус документации (DOCS_HUB)
            <input type="text" disabled placeholder="недоступно">
          </label>
        </div>
        <div class="ctx" style="color:var(--warn); margin-top:8px">
          секция hubs в /api/status отсутствует — недоступно, обновите ядро
        </div>
      </template>
      <div v-if="hubsOk" class="banner busy show">
        ✅ Каталоги сохранены. Применение:
        <code>{{ hubsBanner }}</code>
        <a href="https://docs.docker.com/reference/cli/docker/compose/up/"
           target="_blank" rel="noopener"
           title="что делает --force-recreate">что делает эта команда</a>
      </div>
      <div v-if="hubsErr" class="ctx" style="color:var(--err); margin-top:8px">
        ⛔ {{ hubsErr }}
      </div>
    </div>

    <!-- Подключить агента (Волна 1 п.7) -->
    <div class="card s12">
      <h3>Подключить агента</h3>
      <div class="ctx" style="margin-bottom:10px">
        MCP-сервер компаса: <code>{{ mcpUrl }}</code> — вставьте конфиг в
        клиент агента (адрес должен быть доступен с машины агента).
      </div>
      <div class="row" style="align-items:flex-start; gap:12px">
        <div style="flex:1">
          <div class="muted" style="margin-bottom:4px">JSON (MCP-клиенты)</div>
          <pre style="max-height:110px; white-space:pre-wrap">{{ jsonSnippet }}</pre>
          <button class="btn-ico" style="margin-top:6px" @click="copySnippet(jsonSnippet)">Копировать</button>
        </div>
        <div style="flex:1">
          <div class="muted" style="margin-bottom:4px">Claude Code (CLI)</div>
          <pre style="max-height:110px; white-space:pre-wrap">{{ claudeSnippet }}</pre>
          <button class="btn-ico" style="margin-top:6px" @click="copySnippet(claudeSnippet)">Копировать</button>
        </div>
      </div>
      <div v-if="copyNote" class="ctx" style="color:var(--ok)">✅ {{ copyNote }}</div>
    </div>

    <!-- Эмбеддинг-модель (общая для всех конфигураций) -->
    <div class="card s12">
      <h3>Эмбеддинг-модель · общая для всех конфигураций
        <span class="status-badge" :class="embSource === 'settings' ? 'ok' : 'fast'"
              style="margin-left:8px; text-transform:none">
          {{ embSource === 'settings' ? 'из настроек' : 'из env' }}
        </span>
      </h3>
      <div class="ctx" style="margin-bottom:10px">
        Одна модель эмбеддингов на все конфигурации. Смена модели делает
        существующие векторы несовместимыми — коллекции Qdrant будут удалены,
        векторизацию придётся запустить заново для каждой конфигурации.
      </div>
      <!-- SAFE-5: исходящий поток метаданных при внешнем EMBEDDING_BASE_URL
           (SECURITY.md §4); локальный vLLM/Ollama — предупреждения нет -->
      <div v-if="embExternalHost" class="banner busy show">
        ⚠️ Метаданные методов и тексты запросов уходят на
        <b>{{ embExternalHost }}</b>; для закрытых контуров — локальный
        vLLM/Ollama (SECURITY.md §4)
      </div>
      <div class="emb-grid">
        <label>Base URL
          <input v-model="embForm.base_url" type="text"
                 placeholder="http://localhost:8000/v1">
        </label>
        <label>Модель
          <input v-model="embForm.model" type="text"
                 placeholder="qwen3-embedding-4b">
        </label>
        <label>API-ключ
          <input v-model="embForm.api_key" type="password"
                 placeholder="пусто — из env">
        </label>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="primary" @click="saveEmbedding()"
                :disabled="!embForm.model || embBusy">
          {{ embBusy ? '⏳ Сохраняю…' : 'Сохранить' }}
        </button>
      </div>
      <div v-if="embMsg" class="ctx" style="color:var(--ok); margin-top:8px">
        ✅ {{ embMsg }}
      </div>
      <div v-if="embErr" class="ctx" style="color:var(--err); margin-top:8px">
        ⛔ {{ embErr }}
      </div>
    </div>

  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { get, post, fmt } from '../api/client'
import {
  changedHubs, embeddingWarningHost, hubsAppliedBanner, hubsFromStatus,
} from '../helpers'

const props = defineProps({ status: Object, refresh: Function })

// интервал обновления панели: хранится в браузере (localStorage),
// применяется сразу — App.vue слушает событие compass-refresh-changed
const refreshSec = ref(Number(localStorage.getItem('compass_refresh_sec')) || 5)
const savedNote = ref('')
watch(refreshSec, (sec) => {
  const clamped = Math.min(300, Math.max(5, Number(sec) || 5))
  localStorage.setItem('compass_refresh_sec', String(clamped))
  window.dispatchEvent(new CustomEvent('compass-refresh-changed',
    { detail: clamped }))
  savedNote.value = `интервал: ${clamped} с`
  setTimeout(() => { savedNote.value = '' }, 2500)
})

// эмбеддинг-модель: эффективные значения приходят из /api/status.semantic
// (без api_key); source — из settings.json или env
const embForm = ref({ base_url: '', model: '', api_key: '' })
const embBusy = ref(false)
const embMsg = ref('')
const embErr = ref('')
const embSource = computed(() => props.status?.semantic?.source || 'env')

// SAFE-5 (SECURITY.md §4): внешний base_url — исходящий поток метаданных
// методов; данные — /api/status → semantic.base_url (эффективное значение)
const embExternalHost = computed(() =>
  embeddingWarningHost(props.status?.semantic?.base_url))

// каталоги выгрузок (compass-85s): env-факты из /api/status.hubs; секции
// нет (старое ядро) — поля disabled с пометкой «недоступно, обновите ядро»
const hubsOriginal = computed(() =>
  hubsFromStatus(props.status) || { dumps_hub: '', ext_hub: '', docs_hub: '' })
const hubsAvail = computed(() => hubsFromStatus(props.status) !== null)
const hubsForm = ref({ dumps_hub: '', ext_hub: '', docs_hub: '' })
const hubsBusy = ref(false)
const hubsOk = ref(false)
const hubsErr = ref('')
const hubsBanner = ref('')

// предзаполнение пустых полей из polled статуса; правки пользователя
// (непустые поля) не перетираются при каждом опросе
watch(hubsOriginal, (orig) => {
  for (const key of Object.keys(hubsForm.value)) {
    if (!hubsForm.value[key]) hubsForm.value[key] = orig[key]
  }
}, { immediate: true })

const hubsChangedCount = computed(
  () => Object.keys(changedHubs(hubsOriginal.value, hubsForm.value)).length)

async function saveHubs() {
  hubsErr.value = ''
  hubsOk.value = false
  const payload = changedHubs(hubsOriginal.value, hubsForm.value)
  if (!Object.keys(payload).length) return
  hubsBusy.value = true
  try {
    const r = await post('/api/settings/hubs', payload)
    if (r.error) {
      hubsErr.value = r.error
      return
    }
    hubsOk.value = true
    hubsBanner.value = hubsAppliedBanner(r)
  } catch (e) {
    // 401 приходит как понятная ошибка из client.js
    hubsErr.value = String(e.message || e)
  } finally {
    hubsBusy.value = false
  }
}

watch(() => props.status?.semantic, (sem) => {
  if (sem && !embForm.value.model) {
    embForm.value.base_url = sem.base_url || ''
    embForm.value.model = sem.model || ''
  }
}, { immediate: true })

async function saveEmbedding(recreate = false) {
  embErr.value = ''
  embMsg.value = ''
  if (!embForm.value.model.trim()) return
  embBusy.value = true
  try {
    const body = {
      base_url: embForm.value.base_url.trim(),
      model: embForm.value.model.trim(),
      api_key: embForm.value.api_key,
    }
    if (recreate) body.recreate = true
    const r = await post('/api/settings/embedding', body)
    if (r.needs_confirmation) {
      embBusy.value = false
      if (confirm(r.message)) return saveEmbedding(true)
      return
    }
    if (r.error) {
      embErr.value = r.error
    } else {
      embMsg.value = r.message +
        (r.recreate_error ? ` (Qdrant: ${r.recreate_error})` : '') +
        (r.deleted_collections?.length
          ? ` Удалены коллекции: ${r.deleted_collections.join(', ')}`
          : '')
      props.refresh()
    }
  } catch (e) {
    embErr.value = String(e.message || e)
  }
  embBusy.value = false
}

const rows = computed(() => {
  const s = props.status
  const sem = s?.semantic || {}
  const rt = s?.runtime || {}
  const semText = sem.configured
    ? sem.model + ' · ' + fmt(sem.points) + ' точек'
    : 'выкл'
  const rtText = rt.mode === 'offline' ? 'офлайн'
    : rt.configured ? (rt.reachable !== false ? 'подключён' : 'не отвечает')
    : 'не настроен'
  return [
    ['Версия', 'v' + (s?.version || '—') +
      (verUpdate ? ` → доступна ${verUpdate}` : '')],
    ['Активная конфигурация', s?.alias || '—'],
    ['Конфигураций в реестре', String((s?.configs || []).length)],
    ['Расписание пересборки', s?.schedule_min ? s.schedule_min + ' мин' : 'выкл'],
    ['Семантика', semText],
    ['Рантайм', rtText],
    ['Сборка', s?.build?.building ? 'идёт' : 'свободна'],
  ]
})

// Волна 1 п.8: бейдж новой версии — /api/version/check (кэш, fallback None)
const verUpdate = ref('')
onMounted(async () => {
  try {
    const r = await get('/api/version/check')
    if (r.update_available && r.latest) verUpdate.value = r.latest
  } catch { /* бейдж не показывается — молчаливый fallback */ }
})

// Волна 1 п.7: MCP-снипеты с фактическим адресом панели
const mcpUrl = computed(() => window.location.origin + '/mcp')
const jsonSnippet = computed(() => JSON.stringify(
  { mcpServers: { compass: { url: mcpUrl.value } } }, null, 2))
const claudeSnippet = computed(() =>
  `claude mcp add --transport http compass ${mcpUrl.value}`)
const copyNote = ref('')
async function copySnippet(text) {
  try {
    await navigator.clipboard.writeText(text)
    copyNote.value = 'скопировано'
    setTimeout(() => { copyNote.value = '' }, 2000)
  } catch {
    copyNote.value = ''
  }
}
</script>
