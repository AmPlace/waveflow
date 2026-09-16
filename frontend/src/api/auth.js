import { apiRequest } from './client.js'

export async function fetchSetupStatus() {
  const res = await apiRequest('/api/setup/status')
  return res.json()
}

export async function initializeAdmin({ username, password }) {
  const res = await apiRequest('/api/setup/initialize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return res.json()
}

export async function login({ username, password }) {
  const res = await apiRequest('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return res.json()
}

export async function logout() {
  const res = await apiRequest('/api/auth/logout', { method: 'POST' })
  return res.json()
}

export async function fetchMe() {
  const res = await apiRequest('/api/auth/me')
  return res.json()
}
