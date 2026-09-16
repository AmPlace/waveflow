import test from 'node:test'
import assert from 'node:assert/strict'

import {
  packageCardTitle,
  packageIdentity,
  packageScaleLabel,
  packageSubtitle,
  packageSummary,
  packageTagItems,
} from '../../src/views/marketCardUi.js'

test('Market card title and copy come directly from package display fields', () => {
  const pkg = {
    id: 'custom',
    name: '福建联通 IPTV Plugin',
    description: 'Full package description',
    display: {
      subtitle: 'Package-owned subtitle',
      summary: 'Package-owned summary',
    },
  }
  assert.equal(packageCardTitle(pkg), '福建联通 IPTV Plugin')
  assert.equal(packageSubtitle(pkg), 'Package-owned subtitle')
  assert.equal(packageSummary(pkg), 'Package-owned summary')
})

test('identity only resolves explicit badge, brand, and image values', () => {
  assert.deepEqual(
    packageIdentity({
      id: 'custom',
      name: '福建联通 IPTV',
      region: { province: '福建' },
      operators: ['cucc'],
      display: { badge: { text: 'FX', tone: 'sky' } },
    }),
    { text: 'FX', toneClass: 'market-region-sky', imageUrl: '', iconName: '' },
  )

  assert.deepEqual(packageIdentity({
    id: 'wave-plugin',
    name: 'A name containing waveflow',
    source_origin: 'official',
    display: { identity: { brand: 'waveflow' } },
  }), { text: 'WF', toneClass: 'market-region-violet', imageUrl: '', iconName: '' })

  assert.deepEqual(packageIdentity({
    id: 'neutral',
    name: '福建联通 IPTV',
    region: { province: '福建' },
    operators: ['cucc'],
  }), { text: '', toneClass: 'market-region-neutral', imageUrl: '', iconName: '' })

  assert.deepEqual(packageIdentity({
    id: 'image',
    name: 'Image package',
    display: { identity: { icon: { type: 'image', url: 'https://cdn.example/icon.png' } } },
  }), { text: '', toneClass: 'market-region-neutral', imageUrl: 'https://cdn.example/icon.png', iconName: '' })
})

test('one package tag list is shared by card and detail views', () => {
  const tags = packageTagItems({
    tags: ['Custom first', '央视', 'Custom first'],
  })
  assert.deepEqual(tags, [
    { key: '0:Custom first', label: 'Custom first', accentClass: 'market-tag-neutral' },
    { key: '1:央视', label: '央视', accentClass: 'market-tag-neutral' },
    { key: '2:Custom first', label: 'Custom first', accentClass: 'market-tag-neutral' },
  ])
})

test('explicit badges are not shortened and builtin icons use only registered keys', () => {
  assert.equal(packageIdentity({ display: { badge: { text: 'CUSTOM' } } }).text, 'CUSTOM')
  assert.equal(packageIdentity({ display: { identity: { icon: { type: 'builtin', name: 'waveflow' } } } }).text, 'WF')
  assert.equal(packageIdentity({ name: 'WaveFlow 福建联通', display: { identity: { icon: { type: 'builtin', name: 'unknown' } } } }).text, '')
  assert.equal(packageIdentity({ name: 'YouTube', providers: ['youtube'], regions: [{ province: '福建' }] }).imageUrl, '')
  assert.equal(packageSubtitle({ name: '福建联通', regions: [{ province: '福建' }], operators: ['cucc'] }), '')
})

test('scale only formats explicit count fields and does not invent plugin metadata', () => {
  assert.equal(packageScaleLabel({ channel_count: 42 }), '42 个频道')
  assert.equal(packageScaleLabel({ source_count: 3 }), '')
  assert.equal(packageScaleLabel({ channel_count: 0, source_count: 3 }), '')
  assert.equal(packageScaleLabel({ kind: 'logo_pack', logo_count: 3 }), '3 个台标')
  assert.equal(packageScaleLabel({ plugin: { owned_schemes: [{ scheme: 'custom' }] } }), '')
})

test('missing or zero card metrics never borrow another metric across install states', () => {
  for (const state of [{}, { installed: true }, { installed: true, update_available: true }]) {
    for (const count of [undefined, null, 0, '', -1, NaN]) {
      assert.equal(packageScaleLabel({ ...state, kind: 'playlist', channel_count: count, source_count: 9, logo_count: 12 }), '')
      assert.equal(packageScaleLabel({ ...state, kind: 'logo_pack', logo_count: count, channel_count: 12, source_count: 9 }), '')
    }
    assert.equal(packageScaleLabel({ ...state, kind: 'playlist', channel_count: 617, source_count: 1 }), '617 个频道')
    assert.equal(packageScaleLabel({ ...state, kind: 'logo_pack', logo_count: 12, source_count: 1 }), '12 个台标')
  }
})
