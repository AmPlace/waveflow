import { apiRequest as request } from './client.js'

export async function fetchMarketSummary() {
  const res = await request('/api/admin/market')
  return res.json()
}

export async function refreshMarket(marketUrl = '', { allowPrivate = false } = {}) {
  const res = await request('/api/admin/market/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...(marketUrl ? { market_url: marketUrl } : {}),
      allow_private: allowPrivate,
    }),
    timeout: 30_000,
  })
  return res.json()
}

export async function refreshMarketSource(sourceId) {
  const res = await request('/api/admin/market/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_id: sourceId }),
    timeout: 30_000,
  })
  return res.json()
}

export async function fetchMarketSources() {
  const res = await request('/api/admin/market/sources')
  return res.json()
}

export async function createMarketSource(payload) {
  const res = await request('/api/admin/market/sources', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}

export async function updateMarketSource(id, payload) {
  const res = await request(`/api/admin/market/sources/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}

export async function deleteMarketSource(id) {
  const res = await request(`/api/admin/market/sources/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  return res.json()
}

export async function fetchMarketPackages(filters = {}) {
  // Catalog browsing includes unsupported entries so their failure reason stays visible.
  const params = new URLSearchParams({ supported_only: 'false' })
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  const res = await request(`/api/admin/market/packages${qs ? `?${qs}` : ''}`)
  return res.json()
}

export async function fetchMarketPackage(id) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}`)
  return res.json()
}

export async function previewMarketPackage(id) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}/preview`, {
    method: 'POST',
    timeout: 45_000,
  })
  return res.json()
}

export async function importMarketPackage(id, previewId = '', { reinstall = false } = {}) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preview_id: previewId, prefer_cached_preview: true, reinstall }),
    timeout: 45_000,
  })
  return res.json()
}

export async function updateMarketPackage(id) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}/update`, {
    method: 'POST',
    timeout: 45_000,
  })
  return res.json()
}

export async function updateMarketInstall(id, payload) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}/install`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}

export async function runMarketUpdates({ autoUpdateOnly = false } = {}) {
  const res = await request('/api/admin/market/updates/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ auto_update_only: autoUpdateOnly }),
    timeout: 120_000,
  })
  return res.json()
}

export async function uninstallMarketPackage(id) {
  const res = await request(`/api/admin/market/packages/${encodeURIComponent(id)}/install`, {
    method: 'DELETE',
  })
  return res.json()
}
