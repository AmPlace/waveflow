import test from 'node:test'
import assert from 'node:assert/strict'
import { mock } from 'node:test'
import { execFileSync } from 'node:child_process'

import {
  fetchRadioCatalog,
  fetchRadioProgramme,
  mapRadioStationForTest,
  summarizeRadioCatalogState,
} from '../../src/api/radioStations.js'

test('Desktop catalog and programme requests use the configured credentialed Core origin', () => {
  const moduleUrl = new URL('../../src/api/radioStations.js', import.meta.url).href
  execFileSync(process.execPath, ['--input-type=module', '-e', `
    import assert from 'node:assert/strict'
    globalThis.window = {
      WAVEFLOW_DESKTOP: { apiBase: 'http://127.0.0.1:8000' },
      location: { protocol: 'file:', origin: 'null' },
    }
    const api = await import(${JSON.stringify(moduleUrl)})
    const calls = []
    const fetchImpl = async (url, options) => {
      calls.push({ url, credentials: options.credentials })
      return { ok: true, json: async () => [] }
    }
    await api.fetchRadioCatalog({ fetchImpl })
    await api.fetchRadioProgramme({ radioStationId: 'station', radioSourceId: 'source' }, { fetchImpl })
    assert.equal(calls.length, 2)
    for (const call of calls) {
      assert.equal(new URL(call.url).origin, 'http://127.0.0.1:8000')
      assert.equal(call.credentials, 'include')
    }
  `], { timeout: 5000, stdio: 'pipe' })
})

test('Radio catalog maps explicit station/source identity without upstream URLs', async () => {
  const station = mapRadioStationForTest({
    station_id: 'radio_station_yunting',
    name: '云听新闻',
    country: 'CN',
    group_name: '北京',
    metadata: { subtitle: '午间新闻' },
    sources: [{
      source_id: 'radio_source_1',
      provider_key: 'yunting',
      provider_station_id: 'content-1',
      lifecycle_state: 'active',
      reference: { playback_config: { stream_url: 'https://upstream.invalid/live.m3u8' } },
    }],
  })

  assert.equal(station.id, 'radio_station_yunting')
  assert.equal(station.radioStationId, 'radio_station_yunting')
  assert.equal(station.radioSourceId, 'radio_source_1')
  assert.equal('directUrl' in station, false)
  assert.equal(JSON.stringify(station).includes('upstream.invalid'), false)
})

test('Radio catalog fetch uses the bounded domain endpoint and preserves duplicate names', async () => {
  const calls = []
  const result = await fetchRadioCatalog({
    fetchImpl: async (url) => {
      calls.push(String(url))
      return {
        ok: true,
        json: async () => ({ stations: [
          { station_id: 'radio_a', name: '同名', sources: [{ source_id: 'source_a', lifecycle_state: 'active' }] },
          { station_id: 'radio_b', name: '同名', sources: [{ source_id: 'source_b', lifecycle_state: 'active' }] },
        ] }),
      }
    },
  })

  assert.deepEqual(calls, ['/api/radio/stations'])
  assert.deepEqual(result.stations.map((station) => station.id), ['radio_a', 'radio_b'])
})

test('Radio catalog keeps provider state separate from the station projection', async () => {
  const result = await fetchRadioCatalog({
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        stations: [{
          station_id: 'radio_a',
          name: '旧目录电台',
          catalog_status: 'stale',
          sources: [{ source_id: 'source_a', lifecycle_state: 'stale' }],
        }],
        catalog_states: [{
          owner_identity: 'org.waveflow/yunting',
          status: 'stale',
          station_count: 1,
          last_error: 'must not be needed by the frontend',
        }],
      }),
    }),
  })

  assert.equal(result.status, 'success')
  assert.equal(result.stations.length, 1)
  assert.deepEqual(result.catalogStates, [{
    owner_identity: 'org.waveflow/yunting',
    status: 'stale',
    station_count: 1,
  }])
  assert.equal(summarizeRadioCatalogState(result.stations, result.catalogStates), 'stale')
})

test('Radio catalog maps transport failures to an error without clearing compatibility API semantics', async () => {
  const result = await fetchRadioCatalog({
    fetchImpl: async () => ({ ok: false, status: 503 }),
  })
  assert.deepEqual(result, { status: 'error', errorKind: 'http' })

  const failed = await fetchRadioCatalog({
    fetchImpl: async () => { throw new Error('network unavailable') },
  })
  assert.deepEqual(failed, { status: 'error', errorKind: 'network' })
})

test('Radio catalog timeout is an error while external cancellation remains silent', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const timeoutPromise = fetchRadioCatalog({
      fetchImpl: async (_url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true })
      }),
    })
    mock.timers.tick(15_000)
    assert.deepEqual(await timeoutPromise, { status: 'error', errorKind: 'timeout' })
  } finally {
    mock.timers.reset()
  }

  const controller = new AbortController()
  const cancelledPromise = fetchRadioCatalog({
    signal: controller.signal,
    fetchImpl: async (_url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true })
    }),
  })
  controller.abort()
  assert.deepEqual(await cancelledPromise, { status: 'cancelled' })
})

test('Radio catalog state treats a valid empty snapshot differently from a failed snapshot', () => {
  assert.equal(summarizeRadioCatalogState([], [{ status: 'success', station_count: 0 }]), 'empty')
  assert.equal(summarizeRadioCatalogState([], [{ status: 'failed', station_count: 0 }]), 'error')
  assert.equal(
    summarizeRadioCatalogState([{ catalogStatus: 'degraded' }], []),
    'degraded',
  )
})

test('Radio programme fetch submits only the persisted source_id', async () => {
  const calls = []
  const result = await fetchRadioProgramme({ radioStationId: 'radio_a', radioSourceId: 'source_a' }, {
    fetchImpl: async (url) => {
      calls.push(String(url))
      return { ok: true, json: async () => ({ programmes: [] }) }
    },
  })

  assert.deepEqual(result, { programmes: [] })
  assert.equal(calls[0], '/api/radio/stations/radio_a/programme?source_id=source_a')
})

test('Radio metadata never guesses a type or region from name/group and preserves custom types', () => {
  const raw = { station_id: 'one', name: 'Music News Sports', country: 'ZZ', group_name: '北京', sources: [{source_id:'s'}] }
  const missing = mapRadioStationForTest(raw)
  assert.equal(missing.radioType, '')
  assert.equal(missing.radioRegion, 'ZZ')
  assert.equal('radioGroup' in missing, false)
  assert.deepEqual(missing.tags, ['ZZ','北京'])
  const custom = mapRadioStationForTest({...raw,metadata:{tag:'自定义类型'}})
  assert.equal(custom.radioType, '自定义类型')
  assert.equal(custom.tags.at(-1), '自定义类型')
})

test('programme failure cleans its timeout and Core reads retain credential policy', async () => {
  mock.timers.enable({apis:['setTimeout']})
  let signal
  try {
    assert.equal(await fetchRadioProgramme({radioStationId:'one',radioSourceId:'s'},{fetchImpl:async(_url,options)=>{
      signal=options.signal
      assert.equal(options.credentials,'same-origin')
      throw new Error('offline')
    }}),null)
    mock.timers.tick(10_000)
    assert.equal(signal.aborted,false)
  } finally { mock.timers.reset() }
})
