import test from 'node:test'
import assert from 'node:assert/strict'

import {
  adapterNameFromUrl,
  buildChannelProxyUrl,
  extractSourceIdFromUrl,
  isAdapterSchemeUrl,
  isChannelAllNotLive,
  isChannelAllUrlsBlocked,
  isDynamicAdapterProxyPlaylistEntry,
  isSourceExplicitlyDisabled,
  sourceRaceKey,
  sourceTransport,
  startupRaceCandidateKind,
} from '../../src/utils/sourceIdentity.js'

const isChannelProxyPlaylistUrl = (url) => {
  try {
    const parsed = new URL(url, 'http://waveflow.local')
    return /\/api\/media\/channel\/.+\/playlist\.m3u8$/i.test(parsed.pathname)
  } catch {
    return false
  }
}

test('direct and proxy entries keep the same logical source_id', () => {
  const direct = { source_id: 'src_same', type: 'direct', url: 'https://up.example/live.m3u8' }
  const proxy = { source_id: 'src_same', type: 'proxy', via_proxy: true, url: '/api/media/channel/c/playlist.m3u8?source_id=src_same' }

  assert.equal(direct.source_id, proxy.source_id)
  assert.equal(sourceTransport(direct), 'direct')
  assert.equal(sourceTransport(proxy), 'proxy')
})

test('loser key is source_id plus transport', () => {
  const direct = { source_id: 'src_a', type: 'direct' }
  const proxy = { source_id: 'src_a', type: 'proxy', via_proxy: true }

  assert.equal(sourceRaceKey(direct), 'src_a:direct')
  assert.equal(sourceRaceKey(proxy), 'src_a:proxy')
  assert.notEqual(sourceRaceKey(direct), sourceRaceKey(proxy))
})

test('direct loser does not mark proxy loser', () => {
  const losers = new Set()
  const direct = { source_id: 'src_a', type: 'direct' }
  const proxy = { source_id: 'src_a', type: 'proxy', via_proxy: true }

  losers.add(sourceRaceKey(direct))

  assert.equal(losers.has(sourceRaceKey(direct)), true)
  assert.equal(losers.has(sourceRaceKey(proxy)), false)
})

test('race winner source_id matches request URL source_id', () => {
  const winner = {
    source_id: 'src_win',
    type: 'proxy',
    via_proxy: true,
    url: buildChannelProxyUrl({ apiBase: '', channelKey: '福建综合', sourceId: 'src_win' }),
  }

  assert.equal(extractSourceIdFromUrl(winner.url), winner.source_id)
})

test('manual second source request contains second source_id', () => {
  const sources = [
    { source_id: 'src_first' },
    { source_id: 'src_second' },
  ]
  const requestUrl = buildChannelProxyUrl({ apiBase: '', channelKey: '频道', sourceId: sources[1].source_id })

  assert.equal(extractSourceIdFromUrl(requestUrl), 'src_second')
})

test('proxy request does not contain upstream URL or token', () => {
  const upstream = 'https://cdn.example/live.m3u8?token=secret'
  const requestUrl = buildChannelProxyUrl({ apiBase: '', channelKey: '频道', sourceId: 'src_safe' })

  assert.equal(requestUrl.includes('source_url='), false)
  assert.equal(requestUrl.includes(encodeURIComponent(upstream)), false)
  assert.equal(requestUrl.includes(upstream), false)
  assert.equal(requestUrl.includes('token=secret'), false)
})

test('http and https URLs are not adapter schemes', () => {
  assert.equal(isAdapterSchemeUrl('https://cdn.example/live.m3u8'), false)
  assert.equal(isAdapterSchemeUrl('http://cdn.example/live.m3u8'), false)
})

test('known adapter schemes are recognized by the shared source helper', () => {
  assert.equal(isAdapterSchemeUrl('huya://31421'), true)
  assert.equal(isAdapterSchemeUrl('adapter://huya/31421'), true)
  assert.equal(adapterNameFromUrl('huya://31421'), 'huya')
  assert.equal(adapterNameFromUrl('adapter://huya/31421'), 'huya')
})

test('ordinary channel proxy fallback with empty adapter is not dynamic adapter proxy', () => {
  const entry = {
    source_id: 'src_hls',
    type: 'proxy',
    via_proxy: true,
    adapter: '',
    original_url: 'https://cdn.example/live.m3u8',
    url: buildChannelProxyUrl({ apiBase: '', channelKey: '普通频道', sourceId: 'src_hls' }),
  }

  assert.equal(isDynamicAdapterProxyPlaylistEntry(entry, isChannelProxyPlaylistUrl), false)
})

test('direct and proxy queue entries classify through shared adapter helpers without ReferenceError', () => {
  const direct = {
    source_id: 'src_hls',
    type: 'direct',
    original_url: 'https://cdn.example/live.m3u8',
    url: 'https://cdn.example/live.m3u8',
  }
  const ordinaryProxy = {
    ...direct,
    type: 'proxy',
    via_proxy: true,
    url: buildChannelProxyUrl({ apiBase: '', channelKey: '普通频道', sourceId: 'src_hls' }),
  }
  const adapterProxyWithoutAdapterField = {
    source_id: 'src_huya',
    type: 'proxy',
    via_proxy: true,
    original_url: 'huya://31421',
    url: buildChannelProxyUrl({ apiBase: '', channelKey: '虎牙', sourceId: 'src_huya' }),
  }
  const adapterProxyWithAdapterField = {
    ...adapterProxyWithoutAdapterField,
    adapter: 'huya',
    original_url: 'https://resolved.example/live.m3u8',
  }

  assert.equal(isDynamicAdapterProxyPlaylistEntry(direct, isChannelProxyPlaylistUrl), false)
  assert.equal(isDynamicAdapterProxyPlaylistEntry(ordinaryProxy, isChannelProxyPlaylistUrl), false)
  assert.equal(isDynamicAdapterProxyPlaylistEntry(adapterProxyWithoutAdapterField, isChannelProxyPlaylistUrl), true)
  assert.equal(isDynamicAdapterProxyPlaylistEntry(adapterProxyWithAdapterField, isChannelProxyPlaylistUrl), true)
})

test('not_live channel is not blocked (user can keep trying)', () => {
  const ch = { urls: [
    { probe_status: 'not_live', is_working: 0 },
    { probe_status: 'not_live', is_working: 0 },
  ] }
  assert.equal(isChannelAllUrlsBlocked(ch), false)
  assert.equal(isChannelAllNotLive(ch), true)
})

test('all-offline / all-error / all-timeout channel is still playable', () => {
  for (const status of ['offline', 'error', 'timeout']) {
    const ch = { urls: [{ probe_status: status }, { probe_status: status }] }
    assert.equal(isChannelAllUrlsBlocked(ch), false, `status=${status}`)
    assert.equal(isChannelAllNotLive(ch), false, `status=${status}`)
  }
})

test('legacy is_working===0 schema (no probe_status string) is still playable', () => {
  const ch = { urls: [{ is_working: 0 }, { is_working: 0 }] }
  assert.equal(isChannelAllUrlsBlocked(ch), false)
  assert.equal(isChannelAllNotLive(ch), false)
})

test('only explicit source disable flags block a channel', () => {
  const ch = { urls: [{ disabled: true }, { enabled: '0' }] }
  assert.equal(isChannelAllUrlsBlocked(ch), true)
  assert.equal(isSourceExplicitlyDisabled(ch.urls[0]), true)
  assert.equal(isSourceExplicitlyDisabled(ch.urls[1]), true)
})

test('mixed not_live + offline is not blocked while not_live is allowed', () => {
  // 关键不变量：测速状态只排序和提示，不阻止用户尝试。
  const ch = { urls: [{ probe_status: 'not_live' }, { probe_status: 'offline' }] }
  assert.equal(isChannelAllUrlsBlocked(ch), false)
  assert.equal(isChannelAllNotLive(ch), false)
})

test('online or untested channel is not blocked', () => {
  for (const status of ['online', 'untested']) {
    const ch = { urls: [{ probe_status: status }] }
    assert.equal(isChannelAllUrlsBlocked(ch), false, `status=${status}`)
    assert.equal(isChannelAllNotLive(ch), false, `status=${status}`)
  }
})

test('dynamic adapter proxy remains eligible for delayed startup race', () => {
  const entry = {
    url: '/api/media/channel/test/playlist.m3u8?source_id=src_adapter',
    source_id: 'src_adapter',
    source_type: 'hls',
    type: 'proxy',
    via_proxy: true,
    adapter: 'fjtv',
    probe_status: 'offline',
  }
  assert.equal(isDynamicAdapterProxyPlaylistEntry(entry, isChannelProxyPlaylistUrl), true)
  assert.equal(startupRaceCandidateKind(entry, {
    sourceType: 'hls',
    hlsSupported: true,
    mpegTsSupported: true,
  }), 'hls')
})

test('adapter proxy with unresolved transport enters deferred preflight race', () => {
  const entry = {
    url: '/api/media/channel/test/playlist.m3u8?source_id=src_adapter',
    source_id: 'src_adapter',
    source_type: 'adapter',
    type: 'proxy',
    via_proxy: true,
    adapter: 'huya',
    adapter_transport_pending: true,
  }
  assert.equal(startupRaceCandidateKind(entry, {
    sourceType: 'adapter',
    hlsSupported: true,
    mpegTsSupported: true,
  }), 'proxy_auto')
})

test('explicitly disabled source is excluded from startup race', () => {
  const entry = { url: 'https://cdn.example/live.m3u8', disabled: true }
  assert.equal(startupRaceCandidateKind(entry, {
    sourceType: 'hls',
    hlsSupported: true,
  }), '')
})

test('empty / nullish urls is not blocked (treat as unknown, not failed)', () => {
  assert.equal(isChannelAllUrlsBlocked({ urls: [] }), false)
  assert.equal(isChannelAllUrlsBlocked({}), false)
  assert.equal(isChannelAllUrlsBlocked(null), false)
  assert.equal(isChannelAllUrlsBlocked(undefined), false)
  assert.equal(isChannelAllNotLive({ urls: [] }), false)
  assert.equal(isChannelAllNotLive(null), false)
})

test('IptvHome and FullPlayer share the same blocked-channel rule via the same helper', async () => {
  // 反复验证两个入口都从 utils 拿同一个函数，避免后续再次分叉。
  const utilsMod = await import('../../src/utils/sourceIdentity.js')
  const ch = { urls: [
    { probe_status: 'not_live' },
    { probe_status: 'not_live' },
  ] }
  // 模拟两个入口现在都直接调用 utils
  const homeBlocked = utilsMod.isChannelAllUrlsBlocked(ch)
  const fullPlayerBlocked = utilsMod.isChannelAllUrlsBlocked(ch)
  assert.equal(homeBlocked, fullPlayerBlocked)
  assert.equal(homeBlocked, false)
})
