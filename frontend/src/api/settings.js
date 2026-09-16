import { apiRequest } from './client'

export async function fetchSecuritySettings({ signal } = {}) {
  const res = await apiRequest('/api/admin/settings/security', { signal })
  return res.json()
}

export async function updateSecuritySettings(payload, { signal } = {}) {
  const res = await apiRequest('/api/admin/settings/security', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
    signal,
  })
  return res.json()
}
