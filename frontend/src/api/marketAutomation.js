import { apiRequest } from './client.js'

export async function fetchMarketAutomation({ signal } = {}) {
  const response = await apiRequest('/api/admin/market/automation', { signal })
  return response.json()
}

export async function updateMarketAutomation(payload, { signal } = {}) {
  const response = await apiRequest('/api/admin/market/automation', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  return response.json()
}
