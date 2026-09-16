import assert from 'node:assert/strict'
import test from 'node:test'

import { heightForWidth, IPTV_CARD_RATIOS } from '../../src/utils/iptvCardGeometry.js'

test('IPTV card geometry uses width/height ratios explicitly', () => {
  assert.equal(IPTV_CARD_RATIOS.STANDARD_VISUAL, 16 / 9)
  assert.equal(IPTV_CARD_RATIOS.COMPACT, 1.85)
  // 390px: (390 - 40px page padding - 14px gap) / 2 = 168px.
  assert.ok(Math.abs(heightForWidth(168, IPTV_CARD_RATIOS.COMPACT) - 90.81081) < 0.001)
  // 430px: (430 - 40px page padding - 14px gap) / 2 = 188px.
  assert.ok(Math.abs(heightForWidth(188, IPTV_CARD_RATIOS.COMPACT) - 101.62162) < 0.001)
  assert.ok(Math.abs(heightForWidth(209.6, IPTV_CARD_RATIOS.STANDARD_VISUAL, 44) - 161.9) < 0.1)
})

test('invalid card geometry inputs fail closed to footer height', () => {
  assert.equal(heightForWidth(0, 1.85, 44), 44)
  assert.equal(heightForWidth(167, 0, 44), 44)
  assert.equal(heightForWidth('bad', 1.85), 0)
})
