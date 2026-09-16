import { apiRequest as request } from './client'

export async function fetchSubscriptions() {
  const res = await request('/api/admin/subscriptions')
  return res.json()
}

export async function addSubscription(url, title = '', custom_ua = '', force_proxy = false) {
  const res = await request('/api/admin/subscriptions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, title, custom_ua, force_proxy }),
  })
  return res.json()
}

export async function deleteSubscription(id) {
  await request(`/api/admin/subscriptions/${id}`, { method: 'DELETE' })
}

export async function refreshSubscription(id) {
  const res = await request(`/api/admin/subscriptions/${id}/refresh`, { method: 'POST' })
  return res.json()
}

export async function refreshAllSubscriptions() {
  const res = await request('/api/admin/subscriptions/refresh-all', {
    method: 'POST',
    timeout: 120_000,
  })
  return res.json()
}

export async function fetchChannels(subId, { group = '', search = '' } = {}) {
  const params = new URLSearchParams()
  if (group) params.set('group', group)
  if (search) params.set('search', search)
  const qs = params.toString()
  const res = await request(`/api/admin/subscriptions/${subId}/channels${qs ? '?' + qs : ''}`)
  return res.json()
}

export async function testAllChannels(subId) {
  const res = await request(`/api/admin/subscriptions/${subId}/test-all`, { method: 'POST' })
  return res.json()
}

export async function cancelTest() {
  const res = await request('/api/admin/probes/test-cancel', { method: 'POST' })
  return res.json()
}

export async function fetchTestStatus(subId) {
  const res = await request(`/api/admin/subscriptions/${subId}/test-status`)
  return res.json()
}

// ── 聚合频道（前端用）──

export async function fetchAggregatedChannels({ group = '', search = '', signal } = {}) {
  const params = new URLSearchParams()
  if (group) params.set('group', group)
  if (search) params.set('search', search)
  const qs = params.toString()
  const res = await request(`/api/iptv/channels${qs ? '?' + qs : ''}`, { signal })
  return res.json()
}

export async function testAllGlobal() {
  const res = await request('/api/admin/probes/test-all', { method: 'POST' })
  return res.json()
}

export async function fetchGlobalTestStatus() {
  const res = await request('/api/admin/probes/test-status')
  return res.json()
}

// ── optional source visual metadata (Plugin-owned when available) ──
// The endpoint is generic and keyed by the channel only for projection
// lookup; Core keeps the actual TTL/cache identity source-scoped.
//   GET /api/media/channel/{canonical_key}/visual
export async function fetchChannelVisual(canonicalKey, { signal } = {}) {
  if (!canonicalKey) {
    return {
      ok: true, cover_url: '', stable_cover_url: '', dynamic_cover_url: '', avatar_url: '', title: '', is_live: false,
    }
  }
  const res = await request(`/api/media/channel/${encodeURIComponent(canonicalKey)}/visual`, { timeout: 8_000, signal })
  return res.json()
}
