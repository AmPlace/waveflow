import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'

import {
  disableLogicalChannelEpg,
  lockEpgBinding,
  restoreAutomaticEpgBinding,
  setManualEpgBinding,
  unlockEpgBinding,
} from '../../src/api/epgMatchingManagement.js'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

test('未匹配频道完成手动绑定、解锁、no_epg 与恢复自动的完整管理流程', async () => {
  const logicalChannelId = 'logical/channel 120'
  const selectedIdentity = { epg_source_id: 2, epg_channel_id: 'JP-001' }
  const state = {
    binding: null,
    policy: 'automatic',
  }
  const calls = []

  globalThis.fetch = async (url, options = {}) => {
    const request = { url: String(url), method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null }
    calls.push(request)

    if (request.url.endsWith('/binding') && request.method === 'PUT') {
      assert.deepEqual(request.body, selectedIdentity)
      state.binding = { ...request.body, origin: 'manual', locked: true, status: 'matched' }
      state.policy = 'automatic'
      return jsonResponse({ operation: 'manual_bind', binding: state.binding })
    }
    if (request.url.endsWith('/binding/lock') && request.method === 'DELETE') {
      state.binding.locked = false
      return jsonResponse({ operation: 'unlock', binding: state.binding })
    }
    if (request.url.endsWith('/binding/lock') && request.method === 'PUT') {
      state.binding.locked = true
      return jsonResponse({ operation: 'lock', binding: state.binding })
    }
    if (request.url.endsWith('/no-epg') && request.method === 'PUT') {
      state.binding = null
      state.policy = 'no_epg'
      return jsonResponse({ operation: 'no_epg', policy: { mode: state.policy } })
    }
    if (request.url.endsWith('/restore-automatic') && request.method === 'POST') {
      state.binding = null
      state.policy = 'automatic'
      return jsonResponse({ operation: 'restore_automatic', maintenance: { status: 'success' } })
    }
    return jsonResponse({ detail: { code: 'unexpected_request' } }, 500)
  }

  await setManualEpgBinding(logicalChannelId, selectedIdentity)
  assert.deepEqual(state.binding, { ...selectedIdentity, origin: 'manual', locked: true, status: 'matched' })

  await unlockEpgBinding(logicalChannelId)
  assert.equal(state.binding.locked, false)
  assert.deepEqual(
    { epg_source_id: state.binding.epg_source_id, epg_channel_id: state.binding.epg_channel_id },
    selectedIdentity,
  )

  await lockEpgBinding(logicalChannelId)
  assert.equal(state.binding.locked, true)

  await disableLogicalChannelEpg(logicalChannelId)
  assert.equal(state.binding, null)
  assert.equal(state.policy, 'no_epg')

  await restoreAutomaticEpgBinding(logicalChannelId)
  assert.equal(state.binding, null)
  assert.equal(state.policy, 'automatic')
  assert.deepEqual(calls.map(({ method }) => method), ['PUT', 'DELETE', 'PUT', 'PUT', 'POST'])
  assert.ok(calls.every(({ url }) => url.includes(encodeURIComponent(logicalChannelId))))
})

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
