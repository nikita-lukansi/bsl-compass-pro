<template>
  <div>
    <!-- Сводка -->
    <div class="grid">
      <div class="card s3">
        <h3>Конфигураций</h3>
        <div class="kpi">{{ configs.length }}</div>
        <div class="ctx">в реестре</div>
      </div>
      <div class="card s3">
        <h3>Активная</h3>
        <div class="kpi" style="font-size:18px">{{ activeName }}</div>
        <div class="ctx">{{ activeVersion }}</div>
      </div>
      <div class="card s3">
        <h3>Индексы</h3>
        <div class="kpi" :class="readyCount ? 'ok' : 'err'">{{ readyCount }} / {{ configs.length }}</div>
        <div class="ctx">готовых индексов</div>
      </div>
      <div class="card s3">
        <h3>Очередь сборки</h3>
        <div class="kpi" :class="queue ? 'warn' : ''">{{ queue.length }}</div>
        <div class="ctx">{{ queue.length ? queue.join(', ') : 'пуста' }}</div>
      </div>
    </div>

    <!-- Тулбар -->
    <div class="row" style="margin:16px 0 10px">
      <button class="primary btn-ico" @click="openAdd">
        <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        Добавить
      </button>
      <button class="btn-ico" :disabled="!selected.length" @click="showEdit = true">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        Редактировать
      </button>
      <button class="danger btn-ico" :disabled="!selected.length" @click="doDelete">
        <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
        Удалить
      </button>
      <span class="spacer"></span>
      <button class="btn-ico" :disabled="!selected.length" @click="doActivate">
        <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        Активировать
      </button>
      <div class="dropdown" style="position:relative">
        <button class="btn-ico" :disabled="!selected.length" @click="actionsOpen = !actionsOpen">
          Действия
          <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div v-if="actionsOpen" class="dropdown-menu">
          <button class="dropdown-item" @click="startIndexing">
            <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            Начать индексацию
          </button>
          <button class="dropdown-item" @click="startVectors">
            <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            Начать векторизацию
          </button>
        </div>
      </div>
      <span class="muted" v-if="selected.length">выбрано: {{ selected.length }}</span>
    </div>

    <!-- Реестр -->
    <div class="grid">
      <div class="card s12">
        <table class="rows-clickable">
          <thead>
            <tr>
              <th style="width:32px">
                <input type="checkbox" :checked="allSelected" @change="toggleAll">
              </th>
              <th>имя</th>
              <th>конфигурация</th>
              <th>выгрузка</th>
              <th>режим</th>
              <th>индекс</th>
              <th>векторы</th>
              <th>статус</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in configs" :key="c.id"
                :class="{
                  sel: selected.includes(c.id),
                  activeCfg: c.active,
                }"
                @click="toggleRow(c.id, $event)">
              <td @click.stop>
                <input type="checkbox" :checked="selected.includes(c.id)"
                       @change="toggleRow(c.id, $event)">
              </td>
              <td><b>{{ c.display_name || c.alias }}</b><div class='muted' style='font-size:10px'>{{ c.id }}</div></td>
              <td>
                {{ configLabel(c) }}
                <span class="muted">{{ configPlatform(c) }}</span>
              </td>
              <td class="muted">{{ c.source_root?.split('/').pop() }}</td>
              <td>
                <span :class="['status-badge', modeBadgeClass(c)]">{{ modeBadgeText(c) }}</span>
              </td>
              <td>
                <span :class="['status-badge', indexBadgeClass(c)]">{{ indexBadgeText(c) }}</span>
                <button v-if="c.index_ready && !c.index_in_volume"
                        style="font-size:10px; margin-left:4px"
                        title="F9: перенести индекс в volume /data/index (ext4 — быстрее drvfs)"
                        @click.stop="moveToVolume(c)">→ volume</button>
              </td>
              <td><span :class="['status-badge', vectorsClass(c)]">{{ vectorsText(c) }}</span></td>
              <td><span :class="['status-badge', statusKind(c)]">{{ statusText(c) }}</span></td>
            </tr>
            <tr v-if="!configs.length">
              <td colspan="8" class="muted">реестр пуст — нажмите «Добавить»</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Расширения выбранной конфигурации (Т3) -->
    <div v-if="extCfg" class="grid" style="margin-top:16px">
      <div class="card s12">
        <h3>Расширения · {{ extCfg.display_name || extCfg.id }}</h3>
        <div v-if="!extConfirmed && extStack.length" class="banner busy show" style="margin:8px 0">
          ⚠️ Порядок не подтверждён (задавался вручную) — последовательность
          применения не является фактом; после Пинг-синка синхронизируйте из базы
        </div>
        <div v-for="w in extWarnings" :key="w.kind + (w.extension || '')"
             class="banner busy show" style="margin:8px 0">
          ⚠️ {{ w.text }}
        </div>
        <table v-if="extStack.length">
          <thead>
            <tr><th>№</th><th>имя</th><th>назначение</th><th>позиция</th>
                <th>статус</th><th>перехватов</th><th>заимств.</th>
                <th>порядок</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="e in extStack" :key="e.name" :style="{ cursor: 'default' }">
              <td class="num">{{ e.effective_position }}</td>
              <td><b>{{ e.name }}</b></td>
              <td><span :class="['status-badge', purposeClass(e.purpose)]">{{ purposeLabel(e.purpose) }}</span></td>
              <td class="num">{{ e.position }}</td>
              <td>
                <button style="font-size:11px" @click="toggleStatus(e)"
                        :title="e.status === 'dev' ? 'в разработке — исключён из теней' : 'стабильный'">
                  {{ e.status === 'dev' ? '🛠 dev' : '✓ stable' }}</button>
              </td>
              <td class="num">{{ countsFor(e).overrides }}</td>
              <td class="num">{{ countsFor(e).borrowed }}</td>
              <td>
                <span v-if="e.order_source === 'manual'"
                      style="color:var(--warn)"
                      title="порядок не подтверждён — синхронизируйте из базы">вручную</span>
                <span v-else-if="e.order_source === 'runtime'"
                      style="color:var(--ok)">из базы</span>
                <span v-else class="muted">{{ e.order_source || '—' }}</span>
              </td>
              <td style="white-space:nowrap">
                <button style="font-size:11px" @click="moveExt(e, -1)"
                        title="выше внутри назначения">↑</button>
                <button style="font-size:11px; margin-left:4px" @click="moveExt(e, 1)"
                        title="ниже внутри назначения">↓</button>
                <button class="danger" style="font-size:11px; margin-left:4px"
                        @click="removeExt(e)">удалить</button>
              </td>
            </tr>
            <tr v-if="!extStack.length">
              <td colspan="9" class="muted">стек пуст — добавьте расширения
                (позиция и назначение обязательны)</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="ctx">стек пуст — добавьте расширения кнопкой ниже
          (позиция в списке конфигуратора и назначение обязательны — они
          задают порядок применения)</div>
        <div class="row">
          <button class="primary btn-ico" @click="openExtAdd">
            <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Добавить
          </button>
          <button class="btn-ico" :disabled="extSyncing" @click="doExtSync"
                  title="Пинг живой базы: сверка состава и версий стека (Ф4)">
            ⟳ Синхронизировать из базы
          </button>
        </div>
        <div v-if="extSyncReport" class="ctx" style="margin-top:8px; white-space:pre-line">{{ extSyncReport }}</div>
      </div>
    </div>

    <!-- Модал добавления расширения -->
    <div class="modal-overlay" :class="{ show: extShowAdd }">
      <div class="modal" style="min-width:460px">
        <h3>Добавление расширения — {{ extCfg?.display_name || extCfg?.id }}</h3>

        <label>Имя расширения (как в конфигураторе)</label>
        <input v-model="extName" type="text" placeholder="Расш1">

        <label>Позиция в списке «Расширения конфигуратора» (1 = верх) — обязательна</label>
        <input v-model.number="extPosition" type="number" min="1">

        <label>Назначение — обязательно (задаёт порядок применения)</label>
        <div class="mode-toggle">
          <label class="mode-option" :class="{ on: extPurpose === 'Patch' }">
            <input type="radio" v-model="extPurpose" value="Patch"> Исправление (Patch)
          </label>
          <label class="mode-option" :class="{ on: extPurpose === 'Customization' }">
            <input type="radio" v-model="extPurpose" value="Customization"> Адаптация (Customization)
          </label>
          <label class="mode-option" :class="{ on: extPurpose === 'Addition' }">
            <input type="radio" v-model="extPurpose" value="Addition"> Дополнение (Addition)
          </label>
        </div>

        <label>Статус</label>
        <div class="mode-toggle">
          <label class="mode-option" :class="{ on: extStatus === 'stable' }">
            <input type="radio" v-model="extStatus" value="stable"> stable
          </label>
          <label class="mode-option" :class="{ on: extStatus === 'dev' }">
            <input type="radio" v-model="extStatus" value="dev"> dev (в разработке)
          </label>
        </div>

        <div v-if="extError" class="ctx" style="color:var(--err); margin-top:8px">{{ extError }}</div>
        <div class="actions">
          <button class="primary" @click="addExt">Подтвердить</button>
          <button @click="extShowAdd = false">Отмена</button>
        </div>
      </div>
    </div>

    <!-- Модал редактирования -->
    <div class="modal-overlay" :class="{ show: showEdit }">
      <div class="modal">
        <h3>Редактирование конфигурации</h3>

        <label>Алиас (отображаемое имя)</label>
        <input v-model="editAlias" type="text" placeholder="УТ 11.5 (рабочая)">

        <label>Режим поиска</label>
        <div class="mode-toggle">
          <label class="mode-option" :class="{ on: editMode === 'fast' }">
            <input type="radio" v-model="editMode" value="fast"> Быстрый (SQLite)
          </label>
          <label class="mode-option" :class="{ on: editMode === 'full' }">
            <input type="radio" v-model="editMode" value="full"> Полный (+ Qdrant)
          </label>
        </div>

        <label>ID (неизменяем)</label>
        <input :value="editId" type="text" disabled>
        <label>Выгрузка (только чтение)</label>
        <input :value="editRoot" type="text" disabled>

        <div v-if="editError" class="ctx" style="color:var(--err); margin-top:8px">{{ editError }}</div>
        <div class="actions">
          <button class="primary" @click="saveConfig">Сохранить</button>
          <button @click="showEdit = false">Отмена</button>
        </div>
      </div>
    </div>

    <!-- Модал добавления конфигурации v2: мастер из трёх разделов
         (спека arch-storage.md §3.1: Конфигурация / Документация / Расширения;
         кнопка «Добавить» выполняет всю цепочку) -->
    <div class="modal-overlay" :class="{ show: showAdd }">
      <div class="modal" style="min-width:720px; max-width:780px">
        <h3>Добавление конфигурации</h3>

        <!-- вкладки-разделы мастера -->
        <div class="wiz-tabs">
          <button type="button" :class="{ on: wizTab === 'cfg' }"
                  @click="wizTab = 'cfg'">1 · Конфигурация</button>
          <button type="button" :class="{ on: wizTab === 'docs' }"
                  @click="wizTab = 'docs'">2 · Документация</button>
          <button type="button" :class="{ on: wizTab === 'ext' }"
                  @click="wizTab = 'ext'">3 · Расширения</button>
        </div>

        <!-- ═══ Раздел 1: Конфигурация ═══ -->
        <template v-if="wizTab === 'cfg'">
          <label>Алиас (отображаемое имя)</label>
          <input v-model="addAlias" type="text" placeholder="УТ 11.5 (рабочая)">

          <label>Краткое имя — входит в идентификатор Compass_&lt;краткое&gt;_&lt;uid&gt;
            (латиница/цифры/дефис, 1–16)</label>
          <input v-model="addShort" type="text" placeholder="UT"
                 :class="{ 'wiz-invalid': addShort && !shortNameOk }">
          <div v-if="addShort && !shortNameOk" class="ctx" style="color:var(--err)">
            латиница, цифры, дефис и подчёркивание; 1–16 символов
          </div>

          <label>Режим поиска</label>
          <div class="mode-toggle">
            <label class="mode-option" :class="{ on: addMode === 'fast' }">
              <input type="radio" v-model="addMode" value="fast"> Быстрый (SQLite)
            </label>
            <label class="mode-option" :class="{ on: addMode === 'full' }">
              <input type="radio" v-model="addMode" value="full"> Полный (+ Qdrant)
            </label>
          </div>

          <label style="margin-top:14px">Выгрузка (одиночный выбор)</label>
          <table>
            <thead><tr><th></th><th>папка</th><th>конфигурация</th></tr></thead>
            <tbody>
              <tr v-for="d in dumps" :key="d.source_root"
                  :class="{ sel: addRoot === d.source_root && d.valid }"
                  :style="d.valid ? 'cursor:pointer' : 'opacity:.5'"
                  @click="d.valid && (addRoot = d.source_root)">
                <td style="width:32px">
                  <input v-if="d.valid" type="radio" name="dump"
                         :checked="addRoot === d.source_root"
                         @click.stop="addRoot = d.source_root">
                  <span v-else class="muted">—</span>
                </td>
                <td>{{ d.folder }} <span v-if="!d.valid" class="muted">(пусто — нет дампа)</span></td>
                <td class="muted">
                  <template v-if="d.valid">{{ d.info?.Name }} {{ d.info?.Version }} · {{ d.info?.Platform }}</template>
                  <template v-else>—</template>
                </td>
              </tr>
              <tr v-if="!dumps.length"><td colspan="3" class="muted">выгрузок не найдено</td></tr>
            </tbody>
          </table>

          <div class="row">
            <label class="chk">
              <input v-model="addBuild" type="checkbox">
              Собрать индекс (индексация сразу после добавления)
            </label>
          </div>
          <div v-if="addBuild && addMode === 'full'" class="ctx" style="color:var(--warn)">
            Режим «Полный»: после сборки будет запущена и векторизация
          </div>
        </template>

        <!-- ═══ Раздел 2: Документация ═══ -->
        <template v-else-if="wizTab === 'docs'">
          <label>Документация для этой конфигурации</label>
          <div class="mode-toggle wiz-col">
            <label class="mode-option" :class="{ on: docsMode === 'none' }">
              <input type="radio" v-model="docsMode" value="none"> Без документации
            </label>
            <label class="mode-option" :class="{ on: docsMode === 'existing' }">
              <input type="radio" v-model="docsMode" value="existing"> Выгруженный корпус
            </label>
            <label class="mode-option" :class="{ on: docsMode === 'its' }">
              <input type="radio" v-model="docsMode" value="its"> Выгрузить с ИТС
            </label>
          </div>

          <!-- выбор из уже выгруженных корпусов -->
          <template v-if="docsMode === 'existing'">
            <table v-if="docSources.length" style="margin-top:10px">
              <thead><tr><th style="width:32px"></th><th>корпус</th>
                <th>страниц</th><th>знаков</th></tr></thead>
              <tbody>
                <tr v-for="s in docSources" :key="s.source_id"
                    :class="{ sel: docsSourceId === s.source_id }"
                    style="cursor:pointer"
                    @click="docsSourceId = s.source_id">
                  <td style="width:32px">
                    <input type="radio" name="docsSource"
                           :checked="docsSourceId === s.source_id"
                           @click.stop="docsSourceId = s.source_id">
                  </td>
                  <td><b>{{ s.source_id }}</b> <span class="muted">{{ s.root }}</span></td>
                  <td class="num">{{ fmt(s.pages) }}</td>
                  <td class="num muted">{{ fmt(s.chars) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="ctx" style="margin-top:10px">
              выгруженных корпусов в текущем индексе нет —
              воспользуйтесь «выгрузить с ИТС»
            </div>
            <div class="ctx">
              привязка выбранного корпуса к новой конфигурации появится
              после этапа 2 (бэкенд); сейчас корпус попадает в конфигурацию
              через «выгрузить с ИТС»
            </div>
          </template>

          <!-- выгрузка с ИТС -->
          <template v-else-if="docsMode === 'its'">
            <label>source_id корпуса — подсказки: ka25doc · ut11doc</label>
            <input v-model="docsItsId" type="text" list="wizDocSources"
                   placeholder="ka25doc">
            <datalist id="wizDocSources">
              <option>ka25doc</option>
              <option>ut11doc</option>
            </datalist>
            <div class="ctx">
              сейчас бэкенд выгружает корпус по умолчанию (ka25doc);
              выбранный source_id заработает после этапа 2
            </div>

            <!-- полей кредов ИТС в панели нет — бэкенд читает их из env -->
            <div class="ctx" style="margin-top:10px">
              креды ИТС — переменные ITS_LOGIN/ITS_PASSWORD в .env
            </div>

            <div v-if="addBuild" class="ctx" style="margin-top:10px">
              выгрузка начнётся, когда соберётся индекс — мастер дождётся сборки
            </div>
            <div v-else class="ctx" style="color:var(--warn); margin-top:10px">
              без собранного индекса выгрузка документации завершится ошибкой —
              отметьте «Собрать индекс» в разделе 1
            </div>
          </template>
        </template>

        <!-- ═══ Раздел 3: Расширения ═══ -->
        <template v-else>
          <label>Выгрузки расширений (мультивыбор) —
            имя расширения = имя папки выгрузки</label>
          <table v-if="extDumps.length">
            <thead><tr><th style="width:32px"></th><th>папка</th>
              <th>конфигурация</th></tr></thead>
            <tbody>
              <template v-for="d in extDumps" :key="d.source_root">
                <tr :class="{ sel: extSel.includes(d.source_root) }"
                    style="cursor:pointer" @click="toggleExt(d)">
                  <td style="width:32px">
                    <input type="checkbox"
                           :checked="extSel.includes(d.source_root)"
                           @change="toggleExt(d)">
                  </td>
                  <td>
                    {{ d.folder }}
                    <span class="muted">проверьте, что это расширение</span>
                  </td>
                  <td class="muted">{{ d.info?.Name }} {{ d.info?.Version }}</td>
                </tr>
                <tr v-if="extSel.includes(d.source_root)">
                  <td></td>
                  <td colspan="2" style="padding:8px">
                    <div class="wiz-ext-fields"
                         :class="{ 'wiz-invalid': !extItem(d.source_root).purpose }">
                      <span>
                        <span class="wiz-ext-label">Тип применения —
                          задаёт порядок:</span>
                        <select v-model="extItem(d.source_root).purpose">
                          <option value="">— выберите —</option>
                          <option value="Patch">Исправление</option>
                          <option value="Customization">Адаптация</option>
                          <option value="Addition">Дополнение</option>
                        </select>
                      </span>
                      <span>
                        <span class="wiz-ext-label">Позиция в конфигураторе
                          (1 = верх):</span>
                        <input v-model.number="extItem(d.source_root).position"
                               type="number" min="1" style="width:90px">
                      </span>
                    </div>
                    <div v-if="!extItem(d.source_root).purpose"
                         class="ctx" style="color:var(--err)">
                      укажите тип применения — без него «Добавить» заблокировано
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
          <div v-else class="ctx">выгрузок не найдено</div>
          <div class="ctx">
            признака «расширение» в скане дампов нет — показаны все выгрузки,
            проверьте папки; тип применения и позиция обязательны
            (директива владельца v0.4: они задают порядок применения)
          </div>
        </template>

        <div v-if="addError" class="ctx" style="color:var(--err); margin-top:8px">{{ addError }}</div>
        <div v-if="wizLogLines.length" class="wiz-log">{{ wizLogLines.join('\n') }}</div>
        <div v-if="extIncomplete" class="ctx" style="color:var(--warn)">
          в разделе «Расширения» заполните тип применения и позицию
          для каждого выбранного расширения
        </div>
        <div class="actions">
          <button class="primary" :disabled="wizBusy || extIncomplete"
                  @click="doAddWiz">Добавить</button>
          <button :disabled="wizBusy" @click="showAdd = false">Отмена</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { get, post, fmt } from '../api/client'

const props = defineProps({ status: Object, refresh: Function })

const selected = ref([])
const showEdit = ref(false)
const editAlias = ref('')
// окно добавления v2: мастер из трёх разделов (arch-storage.md §3.1) —
// Конфигурация / Документация / Расширения; «Добавить» гонит всю цепочку
const showAdd = ref(false)
const wizTab = ref('cfg') // 'cfg' | 'docs' | 'ext'
const addAlias = ref('')
const addShort = ref('')
const addMode = ref('fast')
const addRoot = ref('')
const addError = ref('')
const addBuild = ref(true)
const dumps = ref([])
const wizBusy = ref(false) // идёт цепочка добавления — кнопки заблокированы
const wizLogLines = ref([]) // пошаговый лог: '→ шаг…', '✓ ок', '✗ ошибка'
// раздел «Документация»: выгруженный корпус или выгрузка с ИТС
const docsMode = ref('none') // 'none' | 'existing' | 'its'
const docsSourceId = ref('')
const docsItsId = ref('ka25doc')
// раздел «Расширения»: мультивыбор выгрузок; тип применения и позиция
// обязательны для каждого (директива владельца v0.4)
const extSel = ref([]) // source_root выбранных выгрузок
const extDetails = ref({}) // source_root → { purpose, position }
const editMode = ref('fast')
const editError = ref('')
const actionsOpen = ref(false)

// краткое имя: то же правило, что registry.SHORT_NAME_RE (латиница/цифры/_/-, 1–16)
const shortNameOk = computed(
  () => /^[A-Za-z0-9_-]{1,16}$/.test(addShort.value.trim()))

// статус документации приходит в /api/status (ключ docs) — отдельного
// роута /api/docs/status нет, выдумывать нечего
const docSources = computed(() => props.status?.docs?.sources || [])

const configs = computed(() => props.status?.configs || [])
const queue = computed(() => (props.status?.build_queue || []))
const allSelected = computed(() =>
  configs.value.length > 0 && selected.value.length === configs.value.length)

const activeCfg = computed(() => configs.value.find(c => c.active) || null)
const activeName = computed(() =>
  activeCfg.value?.display_name || '—')
const activeVersion = computed(() => {
  const info = activeCfg.value?.config_info || {}
  return [info.Name, info.Version].filter(Boolean).join(' ') || 'нет активной'
})
const readyCount = computed(() => configs.value.filter(c => c.index_ready).length)

const effectiveSelected = computed(() => {
  const active = activeCfg.value
  if (active && !selected.value.includes(active.id)) {
    return [...selected.value, active.id]
  }
  return selected.value
})

// -------------------------------------------------- колонки-статусы

function configLabel(c) {
  const info = c.config_info || {}
  return [info.Name, info.Version].filter(Boolean).join(' ') || '—'
}
function configPlatform(c) {
  const info = c.config_info || {}
  return info.Platform ? ` · ${info.Platform}` : ''
}

function indexBadgeText(c) {
  return c.index_ready ? 'Готов' : 'Не готов'
}
function indexBadgeClass(c) {
  return c.index_ready ? 'ok' : 'err'
}

function modeBadgeText(c) {
  return c.mode === 'full' ? 'Полный' : 'Быстрый'
}
function modeBadgeClass(c) {
  return c.mode === 'full' ? 'full' : 'fast'
}

function vectorsText(c) {
  return c.vectors ? `Готов (${fmt(c.vectors)})` : 'Не готов'
}
function vectorsClass(c) {
  return c.vectors ? 'ok' : 'err'
}

function statusText(c) {
  if (c.active) return 'Активна'
  if (!c.index_ready) return 'Не собрана'
  if (!c.vectors) return 'Нет векторов'
  return 'Готова'
}
function statusKind(c) {
  if (c.active) return 'ok'
  if (!c.index_ready) return 'warn'
  if (!c.vectors) return 'warn'
  return 'ok'
}

// -------------------------------------------------- выбор строк

function toggleRow(id) {
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(id)
}
function toggleAll(event) {
  selected.value = event.target.checked ? configs.value.map(c => c.id) : []
}

// -------------------------------------------------- действия

async function useConfig(id) {
  const r = await post(`/api/config/use?id=${encodeURIComponent(id)}`)
  if (r.active) props.refresh()
  else alert(r.error || 'сбой')
}

async function doActivate() {
  const id = effectiveSelected.value.find(a => {
    const c = configs.value.find(x => x.id === a)
    return c && !c.active
  })
  if (!id) return alert('Активная конфигурация уже выбрана')
  await useConfig(id)
}

async function startIndexing() {
  const alias = effectiveSelected.value[0]
  if (!alias) return
  if (!confirm(`Начать индексацию ${alias}?`)) return
  await post(`/api/config/use?id=${encodeURIComponent(alias)}`)
  await post('/reindex')
  actionsOpen.value = false
  props.refresh()
}

async function startVectors() {
  const alias = effectiveSelected.value[0]
  if (!alias) return
  if (!confirm(`Начать векторизацию ${alias}? (нужен настроенный EMBEDDING_*)`)) return
  await post(`/api/config/use?id=${encodeURIComponent(alias)}`)
  await post('/api/semantic/index')
  actionsOpen.value = false
  props.refresh()
}

async function doDelete() {
  const names = selected.value.map(id => {
    const c = configs.value.find(x => x.id === id)
    return c?.display_name || id
  }).join(', ')
  if (!confirm(`Удалить из реестра: ${names}?\nИндексы на диске останутся.`)) return
  for (const id of [...selected.value]) {
    const r = await post('/api/config/delete', { id })
    if (r.error) { alert(r.error); break }
  }
  selected.value = []
  props.refresh()
}

// -------------------------------------------------- мастер добавления v2

function logLine(text) { wizLogLines.value.push(text) }

/** Обновить последнюю строку лога, если это то же ожидание —
 * чтобы поллинг не засорял лог повторами. */
function logWait(text) {
  const lines = wizLogLines.value
  if (lines.length && lines[lines.length - 1].startsWith('→ ждём'))
    lines[lines.length - 1] = text
  else
    lines.push(text)
}

/** Есть ли в логе шаг с ошибкой — при ошибках окно не закрываем: лог надо прочитать. */
const wizHasErrors = () => wizLogLines.value.some(l => l.startsWith('✗'))

// выгрузки для раздела «Расширения»: валидные папки, кроме самой
// конфигурации из раздела 1; признака расширения в скане нет —
// показываем все с пометкой «проверьте, что это расширение»
const extDumps = computed(() =>
  dumps.value.filter(d => d.valid && d.source_root !== addRoot.value))

function extItem(root) {
  if (!extDetails.value[root]) {
    extDetails.value[root] = { purpose: '', position: 1 }
  }
  return extDetails.value[root]
}

function toggleExt(d) {
  const i = extSel.value.indexOf(d.source_root)
  if (i >= 0) {
    extSel.value.splice(i, 1)
  } else {
    extSel.value.push(d.source_root)
    // позиция по умолчанию — порядковый номер выбора (1 = верх списка)
    extItem(d.source_root).position = extSel.value.length
  }
}

// блокировка «Добавить», пока у выбранного расширения не указаны
// тип применения и позиция (позиция ≥ 1) — директива владельца
const extIncomplete = computed(() => extSel.value.some(root => {
  const it = extDetails.value[root]
  return !it || !it.purpose || !(it.position >= 1)
}))

/** Дождаться сборки индекса новой конфигурации: help_sync пишет в index.db
 * и без готового индекса отклоняется. Опрос /api/status раз в 5 с, потолок
 * 15 минут; прогресс — одной обновляемой строкой. true — индекс готов. */
async function waitIndexReady(cid) {
  logLine('→ ждём сборку индекса (выгрузке документации нужен готовый индекс)…')
  for (let waited = 0; waited < 15 * 60; waited += 5) {
    await new Promise(res => setTimeout(res, 5000))
    let done = false
    try {
      const s = await get('/api/status')
      const building = s.build?.building || s.index?.status === 'building'
      done = !building &&
        (s.configs || []).some(c => c.id === cid && c.index_ready)
    } catch { /* сервер мигнул — продолжаем опрос */ }
    if (done) return true
    logWait(`→ ждём сборку индекса (${waited + 5} с)…`)
  }
  return false
}

async function doAddWiz() {
  addError.value = ''
  if (!addAlias.value.trim() || !addRoot.value || !addShort.value.trim()) {
    addError.value = 'Заполните алиас, краткое имя и выберите выгрузку (раздел 1)'
    wizTab.value = 'cfg'
    return
  }
  if (!shortNameOk.value) {
    addError.value = 'Краткое имя: латиница/цифры/дефис/подчёркивание, 1–16 символов'
    wizTab.value = 'cfg'
    return
  }
  wizLogLines.value = []
  wizBusy.value = true
  try {
    // Шаг 1: конфигурация. При ошибке дальше идти нечем — останавливаемся.
    logLine('→ добавление конфигурации…')
    const r = await post('/api/config/add', {
      short_name: addShort.value.trim(),
      display_name: addAlias.value.trim(),
      source_root: addRoot.value,
      mode: addMode.value,
    })
    if (!r.added) {
      logLine(`✗ ${r.error || 'сбой добавления'}`)
      return
    }
    const cid = r.added
    logLine(`✓ конфигурация ${cid} добавлена и стала активной в панели`)
    props.refresh()

    // Шаг 2 (опция «Собрать индекс»): сборка идёт фоном
    if (addBuild.value) {
      await post(`/api/config/use?id=${encodeURIComponent(cid)}`)
      const b = await post('/reindex')
      logLine(b.error ? `✗ сборка индекса: ${b.error}`
                      : `→ сборка индекса запущена (${b.status || 'в фоне'})`)
      if (addMode.value === 'full' && !b.error) {
        const s = await post('/api/semantic/index')
        logLine(s.error ? `✗ векторизация: ${s.error}`
                        : '→ векторизация запущена (режим «Полный»)')
      }
    }

    // Шаг 3 (раздел «Документация»): выгрузка с ИТС — идёт после сборки,
    // т.к. kb sync пишет в index.db; «выгруженный корпус» на этапе 1
    // только отмечается — привязка появится после этапа 2 (бэкенд)
    if (docsMode.value === 'existing') {
      logLine(docsSourceId.value
        ? `• документация: отмечен корпус ${docsSourceId.value} — ` +
          'привязка заработает после этапа 2 (бэкенд)'
        : '• документация: корпус не выбран — шаг пропущен')
    } else if (docsMode.value === 'its') {
      const sourceId = docsItsId.value.trim() || 'ka25doc'
      if (addBuild.value) {
        const ok = await waitIndexReady(cid)
        if (!ok) logLine('✗ сборка не завершилась за 15 минут — ' +
          'выгрузку документации запустите позже из «Настроек»')
      }
      logLine(`→ запуск выгрузки документации с ИТС (${sourceId})…`)
      const d = await post('/api/docs/sync', { source_id: sourceId })
      if (d.error) logLine(`✗ документация: ${d.error}`)
      else if (d.info) logLine(`• документация: ${d.info}`)
      else logLine(`✓ выгрузка документации запущена в фоне (${d.source_id}); ` +
        'прогресс — вкладка «Обзор»')
    }

    // Шаг 4 (раздел «Расширения»): запись в стек новой конфигурации;
    // имя = имя папки выгрузки; ошибка одного расширения не останавливает
    // остальные — честно в лог
    for (const root of extSel.value) {
      const it = extDetails.value[root] || {}
      const folder = root.split(/[\\/]/).pop()
      const r = await post('/api/config/extension/add', {
        id: cid, name: folder, position: it.position, purpose: it.purpose,
      })
      if (r.error) {
        logLine(`✗ расширение ${folder}: ${r.error}` +
          (r.hint ? ` — ${r.hint}` : ''))
      } else {
        logLine(`✓ расширение ${folder}: ${purposeLabel(it.purpose)}, ` +
          `позиция ${it.position}`)
      }
    }
  } finally {
    wizBusy.value = false
    props.refresh()
    if (!wizHasErrors()) showAdd.value = false
  }
}

async function openAdd() {
  try {
    const r = await get('/api/dumps/available')
    dumps.value = r.dumps || []
  } catch (e) { dumps.value = [] }
  wizTab.value = 'cfg'
  addAlias.value = ''
  addShort.value = ''
  addMode.value = 'fast'
  addRoot.value = ''
  addError.value = ''
  addBuild.value = true
  wizBusy.value = false
  wizLogLines.value = []
  docsMode.value = 'none'
  docsSourceId.value = ''
  docsItsId.value = 'ka25doc'
  extSel.value = []
  extDetails.value = {}
  showAdd.value = true
}

// -------------------------------------------------- редактирование

const selectedCfg = computed(() =>
  configs.value.find(c => c.id === selected.value[0]))

const editId = computed(() => selected.value[0] || '')
const editRoot = computed(() => selectedCfg.value?.source_root || '')
const hasVectors = computed(() => !!selectedCfg.value?.vectors)

watch(selected, list => {
  if (list.length === 1) {
    const c = configs.value.find(x => x.id === list[0])
    editAlias.value = c?.display_name || list[0]
    editMode.value = c?.mode === 'full' ? 'full' : 'fast'
    editError.value = ''
  }
})
watch(showEdit, v => { if (v) editError.value = '' })

async function saveConfig() {
  editError.value = ''
  const id = selected.value[0]
  const r = await post('/api/config/update', {
    id,
    display_name: editAlias.value.trim(),
    mode: editMode.value,
  })
  if (r.error) {
    editError.value = r.error
    return
  }
  showEdit.value = false
  props.refresh()

  if (editMode.value === 'full' && !hasVectors.value &&
      confirm('Режим «Полный» сохранён. Запустить векторизацию сейчас?')) {
    await post(`/api/config/use?id=${encodeURIComponent(id)}`)
    await post('/api/semantic/index')
    props.refresh()
  }
}

// -------------------------------------------------- Т3: секция «Расширения»

const extCfg = ref(null)
const extStack = ref([])
const extIndex = ref([])
const extWarnings = ref([])
const extConfirmed = ref(true)
const extShowAdd = ref(false)
const extName = ref('')
const extPosition = ref(1)
const extPurpose = ref('')
const extStatus = ref('stable')
const extError = ref('')

// целевая конфигурация: выбранная в таблице или активная
const extTarget = computed(() => {
  if (selected.value.length === 1)
    return configs.value.find(c => c.id === selected.value[0]) || null
  return activeCfg.value
})

async function loadExtensions() {
  const cfg = extTarget.value
  extCfg.value = cfg
  if (!cfg) {
    extStack.value = []
    extIndex.value = []
    extWarnings.value = []
    return
  }
  try {
    const r = await get(`/api/config/extensions?id=${encodeURIComponent(cfg.id)}`)
    extStack.value = r.stack || []
    extIndex.value = r.extensions || []
    extWarnings.value = r.warnings || []
    extConfirmed.value = !!r.order_confirmed
  } catch { /* панель живёт */ }
}

watch(() => extTarget.value?.id || null, loadExtensions, { immediate: true })

const PURPOSE_RU = { Patch: 'Исправление', Customization: 'Адаптация', Addition: 'Дополнение' }
function purposeLabel(p) { return PURPOSE_RU[p] || p || '—' }
function purposeClass(p) {
  return p === 'Patch' ? 'err' : p === 'Customization' ? 'warn'
       : p === 'Addition' ? 'ok' : ''
}
function countsFor(e) {
  const c = extIndex.value.find(x => x.name === e.name)
  return { overrides: c?.overrides ?? 0, borrowed: c?.borrowed ?? 0 }
}

// ↑/↓ — смена позиции ВНУТРИ назначения (обмен с соседом в эффективном порядке)
async function moveExt(e, dir) {
  const same = extStack.value.filter(x => x.purpose === e.purpose)
  const i = same.findIndex(x => x.name === e.name)
  const j = i + dir
  if (j < 0 || j >= same.length) return
  const r = await post('/api/config/extension/move',
    { id: extCfg.value.id, name: e.name, position: same[j].position })
  if (r.error) alert(r.error)
  await loadExtensions()
  props.refresh()
}

async function removeExt(e) {
  if (!confirm(`Убрать «${e.name}» из стека конфигурации?`)) return
  const r = await post('/api/config/extension/remove',
    { id: extCfg.value.id, name: e.name })
  if (r.error) alert(r.error)
  await loadExtensions()
  props.refresh()
}

// Ф4: Пинг-синк стека с живой базой — сверка состава и версий; порядок
// из API платформы недоступен (ПорядокДоступен=Ложь) — показывается честно
const extSyncing = ref(false)
const extSyncReport = ref('')
async function doExtSync() {
  if (!extCfg.value) return
  extSyncing.value = true
  extSyncReport.value = ''
  try {
    const r = await post('/api/config/extension/sync',
      { id: extCfg.value.id })
    if (r.error || r.hint) {
      extSyncReport.value = r.error || r.hint
      return
    }
    const lines = []
    if (r.matched?.length) lines.push(`сверено: ${r.matched.join(', ')}`)
    for (const m of r.missing_in_stack || [])
      lines.push(`подключено в базе, нет в стеке: «${m.name}»${m.version ? ` (${m.version})` : ''} — добавьте через extension_add (позиция и назначение у разработчика)`)
    if (r.missing_in_base?.length)
      lines.push(`в стеке, но не подключено в базе: ${r.missing_in_base.join(', ')}`)
    if (r.inactive_in_base?.length)
      lines.push(`отключено в базе: ${r.inactive_in_base.join(', ')}`)
    lines.push(r.order_available
      ? 'порядок применения получен из базы'
      : 'порядок применения из API платформы недоступен — позиции подтверждайте в конфигураторе')
    extSyncReport.value = lines.join('\n')
    await loadExtensions()
  } catch (e) {
    extSyncReport.value = 'синк не удался: ' + e
  } finally {
    extSyncing.value = false
  }
}

// F9/06k: перенос индекса в volume /data/index (ext4 против drvfs)
async function moveToVolume(c) {
  if (!confirm(`Перенести индекс «${c.display_name || c.id}» в volume /data/index?
Для больших конфигураций займёт время (копирование).`)) return
  const r = await post('/api/config/move-index', { id: c.id })
  if (r.moved) { alert(`Индекс перенесён: ${r.to}
${((r.bytes || 0) / 1e9).toFixed(2)} ГБ`); props.refresh() }
  else alert(r.error || 'сбой')
}
async function toggleStatus(e) {
  const next = e.status === 'dev' ? 'stable' : 'dev'
  const r = await post('/api/config/extension/status',
    { id: extCfg.value.id, name: e.name, status: next })
  if (r.error) alert(r.error)
  await loadExtensions()
  props.refresh()
}

function openExtAdd() {
  extName.value = ''
  extPosition.value = extStack.value.length
    ? Math.max(...extStack.value.map(x => x.position)) + 1 : 1
  extPurpose.value = ''
  extStatus.value = 'stable'
  extError.value = ''
  extShowAdd.value = true
}

async function addExt() {
  extError.value = ''
  if (!extName.value.trim())
    return (extError.value = 'имя расширения обязательно')
  if (!extPosition.value || extPosition.value < 1)
    return (extError.value = 'позиция обязательна (1 = верх списка конфигуратора)')
  if (!extPurpose.value)
    return (extError.value = 'назначение обязательно (Исправление/Адаптация/Дополнение — спросите разработчика)')
  const r = await post('/api/config/extension/add', {
    id: extCfg.value.id, name: extName.value.trim(),
    position: extPosition.value, purpose: extPurpose.value,
    status: extStatus.value,
  })
  if (r.error) {
    extError.value = r.hint ? `${r.error} ${r.hint}` : r.error
    return
  }
  extShowAdd.value = false
  await loadExtensions()
  props.refresh()
}
</script>
