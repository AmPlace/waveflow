import assert from 'node:assert/strict'
import test from 'node:test'

import {
  IPTV_CARD_DENSITY_STORAGE_KEY,
  IPTV_CARD_DENSITIES,
  defaultIptvCardDensity,
  normalizeIptvCardDensity,
  readIptvCardDensityPreference,
  writeIptvCardDensityPreference,
} from '../../src/utils/iptvCardDensity.js'

test('IPTV density defaults to Compact below desktop breakpoint and Standard at desktop', () => {
  assert.equal(defaultIptvCardDensity(390), IPTV_CARD_DENSITIES.COMPACT)
  assert.equal(defaultIptvCardDensity(430), IPTV_CARD_DENSITIES.COMPACT)
  assert.equal(defaultIptvCardDensity(1023), IPTV_CARD_DENSITIES.COMPACT)
  assert.equal(defaultIptvCardDensity(1024), IPTV_CARD_DENSITIES.STANDARD)
  assert.equal(defaultIptvCardDensity(1440), IPTV_CARD_DENSITIES.STANDARD)
})

test('IPTV density preference only accepts the two presentation modes', () => {
  assert.equal(normalizeIptvCardDensity('standard'), 'standard')
  assert.equal(normalizeIptvCardDensity('compact'), 'compact')
  assert.equal(normalizeIptvCardDensity('mobile'), '')
  assert.equal(normalizeIptvCardDensity(null), '')
})

test('IPTV density preference persists without a backend field', () => {
  const values = new Map()
  const storage = {
    getItem(key) { return values.get(key) || null },
    setItem(key, value) { values.set(key, value) },
  }

  assert.equal(writeIptvCardDensityPreference('compact', storage), true)
  assert.equal(values.get(IPTV_CARD_DENSITY_STORAGE_KEY), 'compact')
  assert.equal(readIptvCardDensityPreference(storage), 'compact')
  assert.equal(writeIptvCardDensityPreference('invalid', storage), false)
  assert.equal(readIptvCardDensityPreference({ getItem() { throw new Error('storage unavailable') } }), '')
})
