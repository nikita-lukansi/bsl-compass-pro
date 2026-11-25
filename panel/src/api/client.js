// API-клиент: обёртки над fetch с токеном и обработкой ошибок.
// Ошибки get() несут HTTP-статус в err.status — ветки деградации
// (например, «Журнал» за админ-токеном, compass-0x3) классифицируют
// по числу, а не по тексту сообщения.

const adminHeaders = () => {
  const t = localStorage.getItem('compass_admin_token') || ''
  return t ? { 'X-Admin-Token': t } : {}
}

export async function get(path) {
  const r = await fetch(path, { headers: adminHeaders() })
  if (r.status === 401) {
    const e = new Error('Нужен админ-токен: localStorage.setItem("compass_admin_token","...")')
    e.status = 401
    throw e
  }
  if (!r.ok) {
    const e = new Error(`HTTP ${r.status}`)
    e.status = r.status
    throw e
  }
  return r.json()
}

export async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { ...adminHeaders(), 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (r.status === 401) {
    throw new Error('Нужен админ-токен: localStorage.setItem("compass_admin_token","...")')
  }
  return r.json()
}

export const fmt = n => (n ?? 0).toLocaleString('ru-RU')
export const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
