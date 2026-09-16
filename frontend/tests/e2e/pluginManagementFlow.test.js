import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'

import { importMarketPackage } from '../../src/api/market.js'
import { approvePluginPermission, fetchPlugin, fetchPlugins, setPluginOwnership } from '../../src/api/plugins.js'

const originalFetch = globalThis.fetch

afterEach(() => { globalThis.fetch = originalFetch })

test('Market install permission approval 与 Settings ownership rollback 使用同一稳定 API', async () => {
  const state = { installed: false, approved: false, ownership: 'legacy' }
  const calls = []
  globalThis.fetch = async (url, options = {}) => {
    const request = { url: String(url), method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null }
    calls.push(request)
    if (request.url.includes('/market/packages/') && request.url.endsWith('/import')) {
      if (!state.approved) return json({ detail: { code: 'PERMISSION_APPROVAL_REQUIRED', message: 'approval required', details: { permissions: ['network.direct'] } } }, 409)
      state.installed = true
      return json({ ok: true, package_type: 'plugin_package', plugin: 'org.waveflow/ptbtv', active_version: '1.0.0', enabled: true })
    }
    if (request.url.endsWith('/permissions/approve')) { state.approved = true; return json({ approved: [{ name: 'network.direct', risk: 'high' }], pending: [] }) }
    if (request.url.endsWith('/ownership/ptbtv')) { state.ownership = request.body.mode; return json({ scheme: 'ptbtv', mode: state.ownership, plugin: request.body.plugin }) }
    if (request.url.endsWith('/api/admin/plugins')) return json({ plugins: [projection(state)] })
    if (request.url.endsWith('/api/admin/plugins/org.waveflow/ptbtv')) return json(projection(state))
    return json({ detail: { code: 'unexpected_request' } }, 500)
  }

  await assert.rejects(importMarketPackage('official::ptbtv-plugin'), error => error.status === 409)
  await approvePluginPermission('org.waveflow/ptbtv', 'network.direct', 'official::ptbtv-plugin')
  await importMarketPackage('official::ptbtv-plugin')
  assert.equal(state.installed, true)

  assert.equal((await fetchPlugins()).plugins[0].ownership[0].mode, 'legacy')
  await setPluginOwnership('ptbtv', 'plugin', 'org.waveflow/ptbtv')
  assert.equal((await fetchPlugin('org.waveflow/ptbtv')).ownership[0].mode, 'plugin')
  await setPluginOwnership('ptbtv', 'legacy')
  assert.equal(state.ownership, 'legacy')
  assert.deepEqual(calls.filter(item => item.url.includes('/ownership/')).map(item => item.body.mode), ['plugin', 'legacy'])
})

function projection(state) {
  return {
    plugin: 'org.waveflow/ptbtv', display_name: 'PTBTV', version: '1.0.0', enabled: true,
    runtime_available: true, lifecycle_state: 'active', runtime: { type: 'python', environment_status: 'ready', dependencies: [{ name: 'curl-cffi', version: '0.13.0' }] },
    permissions: { requested: [{ name: 'network.direct', risk: 'high' }], approved: state.approved ? [{ name: 'network.direct', risk: 'high' }] : [], pending: state.approved ? [] : [{ name: 'network.direct', risk: 'high' }] },
    owned_schemes: ['ptbtv'], ownership: [{ scheme: 'ptbtv', mode: state.ownership, plugin: state.ownership === 'plugin' ? 'org.waveflow/ptbtv' : '' }],
    market: { package_id: 'official::ptbtv-plugin', update_available: false },
  }
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
}
