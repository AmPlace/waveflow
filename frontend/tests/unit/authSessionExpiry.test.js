import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

import { apiRequest, setUnauthorizedHandler } from '../../src/api/client.js'
import { installSessionExpiryHandling } from '../../src/auth/sessionExpiry.js'
import { useAuthStore } from '../../src/stores/auth.js'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
  setUnauthorizedHandler(null)
})

async function harness(path = '/settings') {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.booted = true
  auth.status = 'authenticated'
  auth.user = { id: 1, username: 'admin', role: 'admin' }
  auth.setup.initialized = true
  auth.setup.anonymousBrowse = true

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/radio', component: {}, meta: {} },
      { path: '/settings', component: {}, meta: { requiresAdmin: true } },
      { path: '/login', component: {}, meta: { authPage: true } },
    ],
  })
  await router.push(path)
  installSessionExpiryHandling(router)
  return { auth, router }
}

function unauthorizedResponse() {
  return new Response(JSON.stringify({ detail: '登录已失效' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('已登录管理员收到 401 后清理 stale auth 并回到登录页', async () => {
  const { auth, router } = await harness('/settings')
  globalThis.fetch = async () => unauthorizedResponse()

  await assert.rejects(
    apiRequest('/api/admin/settings/security'),
    (error) => error.status === 401,
  )
  await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(auth.user, null)
  assert.equal(auth.status, 'anonymous')
  assert.equal(router.currentRoute.value.path, '/login')
  assert.equal(router.currentRoute.value.query.redirect, '/settings')
})

test('匿名可浏览页面的会话失效只降级身份，不强制跳转', async () => {
  const { auth, router } = await harness('/radio')
  globalThis.fetch = async () => unauthorizedResponse()

  await assert.rejects(apiRequest('/api/auth/me'), (error) => error.status === 401)
  await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(auth.user, null)
  assert.equal(auth.status, 'anonymous')
  assert.equal(router.currentRoute.value.path, '/radio')
})

test('未登录时的 401 不会把普通登录失败当作会话过期', async () => {
  const { auth, router } = await harness('/radio')
  auth.user = null
  auth.status = 'login-required'
  globalThis.fetch = async () => unauthorizedResponse()

  await assert.rejects(apiRequest('/api/auth/login', { method: 'POST' }), (error) => error.status === 401)
  await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(auth.status, 'login-required')
  assert.equal(router.currentRoute.value.path, '/radio')
})
