import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { fetchMarketPackages } from '../../src/api/market.js'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

test('Market browsing requests unsupported packages and preserves their error state', async () => {
  const packages = [{ id: 'legacy', supported_in_v1: false, importable: false, unsupported_reason: 'Legacy fields' }]
  globalThis.fetch = async (url) => {
    const params = new URL(url, 'http://localhost').searchParams
    assert.equal(params.get('supported_only'), 'false')
    assert.equal(params.get('search'), 'custom')
    return new Response(JSON.stringify({ packages }))
  }
  assert.deepEqual(await fetchMarketPackages({ search: 'custom' }), { packages })
})

test('explicit supported-only requests remain possible', async () => {
  globalThis.fetch = async (url) => {
    assert.equal(new URL(url, 'http://localhost').searchParams.get('supported_only'), 'true')
    return new Response(JSON.stringify({ packages: [] }))
  }
  await fetchMarketPackages({ supported_only: true })
})
