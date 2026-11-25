import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './style.css'

import Overview from './views/Overview.vue'
import Configurations from './views/Configurations.vue'
import Data from './views/Data.vue'
import Live from './views/Live.vue'
import Backups from './views/Backups.vue'
import Journal from './views/Journal.vue'
import Settings from './views/Settings.vue'

const routes = [
  { path: '/', name: 'overview', component: Overview, label: 'Обзор', icon: '🧭' },
  { path: '/configs', name: 'configs', component: Configurations, label: 'Конфигурации', icon: '🗂' },
  { path: '/data', name: 'data', component: Data, label: 'Данные', icon: '📦' },
  { path: '/live', name: 'live', component: Live, label: 'Живые данные', icon: '📡' },
  { path: '/backups', name: 'backups', component: Backups, label: 'Бэкапы', icon: '💾' },
  { path: '/journal', name: 'journal', component: Journal, label: 'Журнал', icon: '📜' },
  { path: '/settings', name: 'settings', component: Settings, label: 'Настройки', icon: '⚙️' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

const app = createApp(App)
app.use(router)
app.mount('#app')
