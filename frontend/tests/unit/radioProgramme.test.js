import test from 'node:test'
import assert from 'node:assert/strict'

import { currentRadioProgramme, normalizeRadioTimestamp } from '../../src/utils/radioProgramme.js'

test('Radio programme numeric Unix seconds are normalized to milliseconds', () => {
  assert.equal(normalizeRadioTimestamp(1720000000), 1720000000000)
  assert.equal(normalizeRadioTimestamp('1720000000'), 1720000000000)
  assert.equal(normalizeRadioTimestamp('2026-08-14T00:00:00Z'), Date.parse('2026-08-14T00:00:00Z'))
})

test('timed Radio schedule selects the bounded current item, not the first future item', () => {
  const programmes = [
    { title: 'Future', start: 200, end: 300 },
    { title: 'Current', start: 100, end: 200 },
  ]
  assert.equal(currentRadioProgramme(programmes, 50000), null)
  assert.equal(currentRadioProgramme(programmes, 150 * 1000).title, 'Current')
})

test('programme without time bounds keeps explicit current-only fallback semantics', () => {
  const current = currentRadioProgramme(
    [{ provider_programme_id: 'current:1', title: 'Now', updated_at: 100 }],
    100000,
  )
  assert.equal(current?.title, 'Now')
})
