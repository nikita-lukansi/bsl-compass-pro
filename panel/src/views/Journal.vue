<template>
  <div class="grid">
    <div class="card s12">
      <h3>Журнал вызовов тулов · 24 ч</h3>
      <div class="muted">
        вызовов: {{ fmt(stats?.calls_24h) }} · ошибок: {{ fmt(stats?.errors_24h) }}
      </div>
      <div class="row">
        <label class="ctx" style="display:flex; align-items:center; gap:6px; margin-top:0">
          показывать:
          <select v-model.number="limitSel" style="min-width:120px" @change="load">
            <option v-for="n in JOURNAL_LIMITS" :key="n" :value="n">последние {{ n }}</option>
          </select>
        </label>
        <span v-if="denied" class="ctx" style="margin-top:0; color:var(--warn)">
          🔒 журнал доступен с админ-токеном</span>
      </div>
      <div v-if="denied" class="ctx">
        Хвост вызовов виден только владельцу: задайте токен в консоли браузера —
        <code>localStorage.setItem("compass_admin_token", "…")</code> — и обновите страницу.
        Счётчики за 24 ч остаются из /api/status и без токена.
      </div>
      <table v-else style="margin-top:8px">
        <thead><tr><th>UTC</th><th>тул</th><th>мс</th><th>символов</th><th>статус</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="i" :style="{ cursor: 'default' }">
            <td class="num">{{ r.ts.slice(11, 19) }}</td>
            <td>{{ r.tool }}</td>
            <td class="num">{{ r.ms ?? '—' }}</td>
            <td class="num">{{ r.result_chars != null ? fmt(r.result_chars) : '—' }}</td>
            <td :style="{ color: r.ok ? '' : 'var(--err)' }">{{ r.ok ? 'ok' : 'ERR' }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="5" class="muted">—</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { get, fmt } from '../api/client'
import { JOURNAL_LIMITS, journalRows, journalDenied } from '../helpers'

const props = defineProps({ status: Object })

// счётчики 24 ч остаются из /api/status (journal.stats_24h) — без токена
const stats = computed(() => props.status?.journal)

// Хвост вызовов — отдельный запрос за админ-токеном (compass-0x3):
// security-волна 2 (d8bc9eb) убрала recent из /api/status, источник —
// GET /api/journal?limit=N (X-Admin-Token шлёт api-клиент глобально).
const rows = ref([])
const limitSel = ref(10)
const denied = ref(false)
let timer = null

async function load() {
  try {
    const r = await get(`/api/journal?limit=${limitSel.value}`)
    rows.value = journalRows(r)
    denied.value = false
  } catch (e) {
    // 401/403 — нет токена: сообщение вместо пустой таблицы;
    // сеть/5xx — прежний паттерн тишины: последние строки остаются
    if (journalDenied(e)) denied.value = true
  }
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>
