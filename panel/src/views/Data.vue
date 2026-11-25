<template>
  <div class="grid">
    <div class="card s6">
      <h3>Зарегистрировать готовый индекс</h3>
      <div class="row">
        <input v-model="regAlias" type="text" placeholder="алиас (short_name)" style="width:150px">
        <input v-model="regPath" type="text" placeholder="путь к index.db" style="width:280px">
        <button class="primary" @click="registerIdx(regAlias, regPath)">Зарегистрировать</button>
      </div>
      <div class="muted" style="margin-top:6px">
        Файл index.db от другого компаса или бэкапа — регистрируется в реестре
        без пересборки (алиас: латиница/цифры/дефис, 1–16 символов).
      </div>
    </div>
    <!-- Внутренняя миграция векторов из Community — не показываем
         в публичной панели (решение техлида: API /api/data/migrate-vectors
         не выпиливаем, скрыт только UI). Логика — ниже в скрипте. -->
    <!--
    <div class="card s6">
      <h3>Миграция векторов из Community</h3>
      <div class="row" style="margin-top:0">
        <input v-model="migUrl" type="text" placeholder="http://host:8120 (Qdrant Community)" style="width:240px">
        <input v-model="migCol" type="text" placeholder="code" value="code" style="width:80px">
        <button class="primary" @click="doMigrate">Перенести</button>
      </div>
      <div class="muted" style="margin-top:6px">
        Читает векторы из Community Qdrant, пересчитывает ID под Pro,
        пишет в cfg_{{ alias }}_{{ migCol }}. Векторы готовые — ни копейки API.
      </div>
      <div v-if="migStatus" class="ctx">{{ migStatus }}</div>
    </div>
    -->
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { get, post, fmt } from '../api/client'

const props = defineProps({ status: Object, refresh: Function })

const regAlias = ref('')
const regPath = ref('')
// Блок внутренней миграции из Community — карточка скрыта в шаблоне
// (не показываем в публичной панели); код оставлен, API не выпиливаем.
const migUrl = ref('')
const migCol = ref('code')
const migStatus = ref('')

const alias = computed(() => props.status?.alias || 'main')

async function registerIdx(a, p) {
  if (!a || !p) return alert('заполните алиас и путь')
  const r = await post('/api/data/register', { alias: a, index_db: p })
  if (r.registered) props.refresh()
  else alert(r.error || 'сбой')
}
async function doMigrate() {
  if (!migUrl.value) return alert('укажите URL Qdrant Community')
  if (!confirm(`Перенести векторы из ${migUrl.value}/${migCol.value}?`)) return
  migStatus.value = '⏳ запускаю…'
  const r = await post('/api/data/migrate-vectors', {
    source_url: migUrl.value, collection: migCol.value,
  })
  migStatus.value = r.status === 'started'
    ? '⏳ миграция запущена (минуты на 200k+)'
    : (r.error || 'сбой')
}
</script>
