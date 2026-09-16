import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  createLatestMarketSourceProjection,
  marketSourceDraft,
  normalizeMarketSourceBoolean,
} from '../../src/views/marketSourceUi.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

test('Market source enabled normalizes backend boolean, numeric, and string values', () => {
  assert.equal(normalizeMarketSourceBoolean(true), true)
  assert.equal(normalizeMarketSourceBoolean(false), false)
  assert.equal(normalizeMarketSourceBoolean(1), true)
  assert.equal(normalizeMarketSourceBoolean(0), false)
  assert.equal(normalizeMarketSourceBoolean('1'), true)
  assert.equal(normalizeMarketSourceBoolean('0'), false)
  assert.equal(normalizeMarketSourceBoolean('true'), true)
  assert.equal(normalizeMarketSourceBoolean('false'), false)
  assert.equal(normalizeMarketSourceBoolean(undefined), false)
})

test('server-confirmed save and reload keep the checkbox model enabled', () => {
  const initial = marketSourceDraft({ id: 7, name: 'Official', enabled: 0 })
  const saved = marketSourceDraft({ id: 7, name: 'Official', enabled: 1 })
  const reloaded = marketSourceDraft({ id: 7, name: 'Official', enabled: true })

  assert.equal(initial.enabled, false)
  assert.equal(saved.enabled, true)
  assert.equal(reloaded.enabled, true)
  assert.equal(saved.persisted.enabled, true)
  assert.equal(reloaded.persisted.enabled, true)
})

test('latest source projection rejects stale summary responses', () => {
  let rendered = []
  const projection = createLatestMarketSourceProjection((sources) => { rendered = sources })

  const summaryRequest = projection.begin()
  const sourceRequest = projection.begin()
  assert.equal(projection.publish(sourceRequest, [{ id: 7, enabled: 1 }]), true)
  assert.equal(projection.publish(summaryRequest, [{ id: 7, enabled: 0 }]), false)
  assert.equal(rendered[0].enabled, true)
})

test('catalog status and bundled fallback do not overwrite source enabled state', () => {
  const projected = []
  const projection = createLatestMarketSourceProjection((sources) => projected.push(sources[0]))

  const failedRefresh = projection.begin()
  projection.publish(failedRefresh, [{ id: 7, enabled: 1, last_status: 'error' }])
  const bundledFallback = projection.begin()
  projection.publish(bundledFallback, [{ id: 7, enabled: 1, last_status: 'bundled' }])
  const disabledSource = projection.begin()
  projection.publish(disabledSource, [{ id: 7, enabled: 0, last_status: 'bundled' }])

  assert.deepEqual(projected.map(source => source.enabled), [true, true, false])
})

test('Market template binds the normalized source model and uses the save response', () => {
  const market = fs.readFileSync(path.join(frontendRoot, 'src/views/MarketView.vue'), 'utf8')
  assert.match(market, /v-model="source\.enabled"/)
  assert.match(market, /applyConfirmedMarketSource\(result\)/)
  assert.match(market, /已保存，但 Market 源状态刷新失败，请稍后重试/)
})
