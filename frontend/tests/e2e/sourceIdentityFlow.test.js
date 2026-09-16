import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildChannelProxyUrl,
  extractSourceIdFromUrl,
  sourceRaceKey,
  sourceTransport,
} from '../../src/utils/sourceIdentity.js'

function proxyEntry(source) {
  return {
    ...source,
    type: 'proxy',
    via_proxy: true,
    url: buildChannelProxyUrl({ apiBase: '', channelKey: '福建综合', sourceId: source.source_id }),
  }
}

test('race winner, network request, and UI selected source_id stay aligned', () => {
  const sourceA = { source_id: 'src_a', original_url: 'https://a.example/live.m3u8' }
  const sourceB = { source_id: 'src_b', original_url: 'https://b.example/live.m3u8' }
  const queue = [
    { ...sourceA, type: 'direct', url: sourceA.original_url },
    proxyEntry(sourceA),
    { ...sourceB, type: 'direct', url: sourceB.original_url },
    proxyEntry(sourceB),
  ]

  const losers = new Set([sourceRaceKey(queue[0])])
  const winner = queue[1]
  const uiSelected = winner
  const networkSourceId = extractSourceIdFromUrl(winner.url)

  assert.equal(losers.has(sourceRaceKey(queue[1])), false)
  assert.equal(sourceTransport(winner), 'proxy')
  assert.equal(winner.source_id, 'src_a')
  assert.equal(networkSourceId, winner.source_id)
  assert.equal(uiSelected.source_id, winner.source_id)
})

test('manual switching between source B and source A proxy keeps requests exact', () => {
  const sourceAProxy = proxyEntry({ source_id: 'src_a', original_url: 'https://a.example/live.m3u8?token=hidden' })
  const sourceBProxy = proxyEntry({ source_id: 'src_b', original_url: 'https://b.example/live.m3u8' })

  assert.equal(extractSourceIdFromUrl(sourceBProxy.url), 'src_b')
  assert.equal(extractSourceIdFromUrl(sourceAProxy.url), 'src_a')
  assert.equal(sourceAProxy.url.includes('token=hidden'), false)
  assert.equal(sourceBProxy.url.includes('https://b.example'), false)
})

test('re-entering a channel keeps stable source_id values', () => {
  const firstLoad = [
    proxyEntry({ source_id: 'src_a', original_url: 'https://a.example/live.m3u8' }),
    proxyEntry({ source_id: 'src_b', original_url: 'https://b.example/live.m3u8' }),
  ]
  const secondLoad = [
    proxyEntry({ source_id: 'src_a', original_url: 'https://a.example/live.m3u8?token=new' }),
    proxyEntry({ source_id: 'src_b', original_url: 'https://b.example/live.m3u8' }),
  ]

  assert.deepEqual(
    secondLoad.map((entry) => entry.source_id),
    firstLoad.map((entry) => entry.source_id),
  )
})
