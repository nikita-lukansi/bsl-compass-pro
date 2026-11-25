<template>
  <div class="grid">
    <!-- Хранилище слепков -->
    <div class="card s8">
      <h3>Слепки · {{ bkAlias || 'активная конфигурация' }}</h3>
      <div class="ctx" style="margin-bottom:10px">
        Каталог: <code class="num">{{ hub || '—' }}</code>
        <span v-if="hub" class="muted"> · в рабочей папке (BACKUPS_HUB переопределяет)</span>
      </div>
      <table>
        <thead><tr><th>создан</th><th>методов</th><th>модулей</th><th></th></tr></thead>
        <tbody>
          <tr v-for="b in backups" :key="b.name" :style="{ cursor: 'default' }">
            <td>{{ fmtWhen(b.created_at) }}</td>
            <td class="num">{{ b.counts?.methods ?? '—' }}</td>
            <td class="num">{{ b.counts?.modules ?? '—' }}</td>
            <td>
              <button @click="doRestore(b.name)">Восстановить</button>
              <button style="margin-left:4px"
                      @click="doRestoreSemantic(b.name)" title="снапшот Qdrant → коллекция реестра">Семантику</button>
            </td>
          </tr>
          <tr v-if="!backups.length"><td colspan="4" class="muted">слепков ещё нет — создайте первый</td></tr>
        </tbody>
      </table>
      <div class="row">
        <button class="primary btn-ico" @click="doBackup" :disabled="!bkAlias">
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
          Создать слепок
        </button>
        <span class="muted">хранятся последние {{ keep || 3 }} · VACUUM INTO без остановки читателей</span>
      </div>
    </div>

    <!-- Inbox -->
    <div class="card s4">
      <h3>Inbox · принесённые</h3>
      <div class="ctx" style="margin-bottom:10px">
        Каталог: <code class="num">{{ inboxHub || '—' }}</code>
      </div>
      <div v-for="b in inbox" :key="b.dir" class="row" style="margin-top:6px">
        <span>{{ b.alias }} · <span class="muted">{{ b.dir }}</span></span>
        <button @click="doApplyInbox(b.dir)">Восстановить</button>
      </div>
      <div v-if="!inbox.length" class="ctx">
        пусто — положите каталог с manifest.json в Inbox и обновите страницу
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { get, post } from '../api/client'

const props = defineProps({ status: Object, refresh: Function })
const backups = ref([])
const inbox = ref([])
const hub = ref('')
const inboxHub = ref('')
const bkAlias = ref('')
const keep = ref('')

function fmtWhen(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function load() {
  try {
    const [bk, inb] = await Promise.all([get('/api/backups'), get('/api/inbox')])
    backups.value = bk.backups || []
    hub.value = bk.hub || ''
    bkAlias.value = bk.alias || ''
    keep.value = bk.keep || ''
    inbox.value = inb.inbox || []
  } catch (e) {}
}
onMounted(load)

async function doBackup() {
  if (!confirm('Создать слепок активной конфигурации?')) return
  const r = await post('/api/backup')
  if (r.created) { load(); props.refresh() }
  else alert(r.error || 'сбой')
}
async function doRestore(name) {
  if (!confirm(`ВОССТАНОВИТЬ слепок ${name}? Индекс будет заменён.`)) return
  const r = await post(`/api/backup/restore?dir=${encodeURIComponent(name)}`)
  if (r.restored) { load(); props.refresh() }
  else alert(r.error || 'сбой')
}
// F7/vlr: восстановить семантику из слепка — снапшот Qdrant загружается
// в иммутабельную коллекцию реестра (POST /api/backups/restore-semantic)
async function doRestoreSemantic(name) {
  const cid = (props.status?.configs || []).find(c => c.active)?.id || ''
  if (!cid) { alert('активная конфигурация не определена'); return }
  if (!confirm(`Восстановить семантику из ${name}? Коллекция Qdrant будет перезаписана.`)) return
  const r = await post('/api/backups/restore-semantic', { id: cid, dir: name })
  if (r.restored) { alert(`Семантика восстановлена в «${r.restored}»` + (r.note ? `
${r.note}` : '')) }
  else alert(r.error || 'сбой')
}
async function doApplyInbox(dir) {
  if (!confirm(`Восстановить принесённый слепок ${dir}?`)) return
  const r = await post(`/api/inbox/apply?dir=${encodeURIComponent(dir)}`)
  if (r.restored) { load(); props.refresh() }
  else alert(r.error || 'сбой')
}
</script>
