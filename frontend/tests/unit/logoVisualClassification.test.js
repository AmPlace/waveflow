import assert from 'node:assert/strict'
import test from 'node:test'

import { classifyLogoMode } from '../../src/composables/useLogoVisual.js'

test('large 16:9-like artwork remains cover', () => {
  assert.equal(classifyLogoMode(640, 360, { enableWide: true }), 'cover')
})

test('wide marks use contain-only wide mode when enabled', () => {
  for (const [width, height] of [[3264, 609], [290, 52], [512, 137], [108, 37], [234, 75]]) {
    assert.equal(classifyLogoMode(width, height, { enableWide: true }), 'wide')
  }
})

test('ordinary badge assets and Radio default remain badge', () => {
  assert.equal(classifyLogoMode(640, 320, { enableWide: true }), 'badge')
  assert.equal(classifyLogoMode(3264, 609), 'badge')
  assert.equal(classifyLogoMode(300, 180, { enableWide: true }), 'badge')
})
