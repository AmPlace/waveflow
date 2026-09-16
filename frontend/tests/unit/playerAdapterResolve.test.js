import test from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'

import { usePlayerStore } from '../../src/stores/player.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

async function flushTasks() {
  await new Promise((resolve) => setImmediate(resolve))
  await Promise.resolve()
}

function installFetchStub(payloads) {
  const calls = []
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    const next = payloads.shift()
    if (next instanceof Error) throw next
    return {
      ok: next?.ok !== false,
      status: next?.status || 200,
      json: async () => next?.body || next || {},
    }
  }
  return calls
}

function adapterChannel(source = {}) {
  return {
    canonical_key: '虎牙',
    urls: [{
      url: 'huya://31421',
      source_id: 'src_huya',
      source_type: 'adapter',
      enabled: true,
      ...source,
    }],
  }
}

test('progressive 模式先发布 direct candidate，不等待慢 adapter resolve', async () => {
  setActivePinia(createPinia())
  const response = deferred()
  const calls = []
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    return await response.promise
  }
  const store = usePlayerStore()
  const play = store.playIptvChannel({
    canonical_key: 'mixed-progressive',
    urls: [
      { url: 'https://cdn.example/live.m3u8', source_id: 'src-direct', source_type: 'hls' },
      { url: 'huya://31421', source_id: 'src-adapter', source_type: 'adapter' },
    ],
  }, { progressive: true })

  assert.equal(store.currentIptvChannel.canonical_key, 'mixed-progressive')
  const currentChannel = store.currentIptvChannel
  assert.equal(store.iptvUrls[0].source_id, 'src-direct')
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.equal(calls.length, 1)

  response.resolve({
    ok: true,
    json: async () => ({
      ok: true,
      url: 'https://cdn.example/adapter.flv',
      source_type: 'http_flv',
      direct_playable: true,
    }),
  })
  await play
  await flushTasks()

  assert.ok(store.iptvUrls.some((entry) => entry.source_id === 'src-adapter' && entry.type === 'direct'))
  assert.equal(store.currentIptvChannel, currentChannel)
  assert.equal(store.pendingIptvChannel, null)
})

test('progressive adapter resolve 使用最多 3 个并发 worker，并按完成顺序补入队列', async () => {
  setActivePinia(createPinia())
  const responses = new Map()
  const calls = []
  globalThis.fetch = async (url) => {
    const sourceId = new URL(url, 'http://waveflow.test').searchParams.get('source_id')
    calls.push(sourceId)
    const pending = deferred()
    responses.set(sourceId, pending)
    return await pending.promise
  }
  const store = usePlayerStore()
  const play = store.playIptvChannel({
    canonical_key: 'parallel-progressive',
    urls: ['a', 'b', 'c', 'd'].map((id) => ({
      url: `huya://${id}`,
      source_id: `src-${id}`,
      source_type: 'adapter',
    })),
  }, { progressive: true })

  await play
  assert.deepEqual(calls.sort(), ['src-a', 'src-b', 'src-c'])
  responses.get('src-b').resolve({ ok: true, json: async () => ({ ok: true, url: 'https://cdn.example/b.flv', source_type: 'http_flv' }) })
  await flushTasks()
  assert.equal(calls.length, 4)
  assert.ok(store.iptvUrls.some((entry) => entry.source_id === 'src-b' && entry.type === 'direct'))

  for (const id of ['src-a', 'src-c', 'src-d']) {
    responses.get(id)?.resolve({ ok: true, json: async () => ({ ok: true, url: `https://cdn.example/${id}.flv`, source_type: 'http_flv' }) })
  }
  await flushTasks()
})

test('progressive 旧频道 resolve 晚返回不得覆盖最新频道', async () => {
  setActivePinia(createPinia())
  const lateA = deferred()
  globalThis.fetch = async () => await lateA.promise
  const store = usePlayerStore()
  const playA = store.playIptvChannel({
    canonical_key: 'channel-a',
    urls: [{ url: 'huya://a', source_id: 'src-a', source_type: 'adapter' }],
  }, { progressive: true })
  await store.playIptvChannel({
    canonical_key: 'channel-b',
    urls: [{ url: 'https://cdn.example/b.m3u8', source_id: 'src-b', source_type: 'hls' }],
  }, { progressive: true })

  lateA.resolve({ ok: true, json: async () => ({ ok: true, url: 'https://cdn.example/a.flv', source_type: 'http_flv' }) })
  await playA
  await flushTasks()

  assert.equal(store.currentIptvChannel.canonical_key, 'channel-b')
  assert.equal(store.iptvUrls[0].source_id, 'src-b')
})

test('adapter source resolves to direct plus source_id proxy fallback by default', async () => {
  setActivePinia(createPinia())
  const calls = installFetchStub([{
    ok: true,
    source_id: 'src_huya',
    url: 'https://tx.flv.huya.com/live/room.flv?token=secret',
    source_type: 'http_flv',
    direct_playable: true,
    requires_proxy: false,
    volatile_url: true,
    proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
  }])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel())

  assert.equal(calls.length, 1)
  assert.match(calls[0], /\/api\/media\/channel\/%E8%99%8E%E7%89%99\/resolve\?source_id=src_huya/)
  assert.equal(store.iptvUrls.length, 2)
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.equal(store.iptvUrls[0].source_id, 'src_huya')
  assert.equal(store.iptvUrls[0].source_type, 'http_flv')
  assert.equal(store.iptvUrls[0].adapter_volatile_url, true)
  assert.match(store.iptvUrls[0].adapter_source_url, /\/api\/media\/channel\/%E8%99%8E%E7%89%99\/resolve\?source_id=src_huya/)
  assert.equal(store.iptvUrls[1].type, 'direct')
  assert.equal(store.iptvUrls[1].via_proxy, true)
  assert.equal(store.iptvUrls[1].source_id, 'src_huya')
  assert.equal(store.iptvUrls[1].url.includes('token=secret'), false)
})

test('provider resolve revision mismatch never publishes a stale candidate', async () => {
  setActivePinia(createPinia())
  const calls = installFetchStub([{
    ok: true,
    body: {
      ok: true,
      source_id: 'src_stale',
      source_revision: 'revision-2',
      url: 'https://cdn.example/old-revision.m3u8',
      source_type: 'hls',
      direct_playable: true,
    },
  }])
  const store = usePlayerStore()

  await store.playIptvChannel({
    canonical_key: 'revision-channel',
    urls: [{
      url: 'huya://31421',
      source_id: 'src_stale',
      source_type: 'adapter',
      source_revision: 'revision-1',
    }],
  })

  assert.match(calls[0], /expected_source_revision=revision-1/)
  assert.equal(store.iptvUrls.length, 0)
  assert.equal(store.playbackError, '没有可播放的源')
})

test('probe proxy_required_hint does not suppress adapter direct when adapter allows it', async () => {
  setActivePinia(createPinia())
  installFetchStub([{
    ok: true,
    source_id: 'src_huya',
    url: 'https://tx.flv.huya.com/live/room.flv?token=secret',
    source_type: 'http_flv',
    direct_playable: true,
    requires_proxy: false,
    proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
  }])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel({ proxy_required_hint: 1 }))

  assert.equal(store.iptvUrls.length, 2)
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.equal(store.iptvUrls[0].via_proxy, undefined)
  assert.equal(store.iptvUrls[0].source_type, 'http_flv')
  assert.equal(store.iptvUrls[1].via_proxy, true)
  assert.equal(store.iptvUrls[1].source_type, 'http_flv')
})

test('market force_proxy suppresses adapter direct even when adapter allows it', async () => {
  setActivePinia(createPinia())
  installFetchStub([{
    ok: true,
    source_id: 'src_huya',
    url: 'https://tx.flv.huya.com/live/room.flv',
    source_type: 'http_flv',
    direct_playable: true,
    requires_proxy: false,
    proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
  }])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel({ force_proxy: true }))

  assert.equal(store.iptvUrls.length, 1)
  assert.equal(store.iptvUrls[0].type, 'proxy')
  assert.equal(store.iptvUrls[0].via_proxy, true)
})

test('adapter requires_proxy is a hard constraint over market/default direct behavior', async () => {
  setActivePinia(createPinia())
  installFetchStub([{
    ok: true,
    source_id: 'src_huya',
    url: 'https://tx.flv.huya.com/live/room.flv',
    source_type: 'http_flv',
    direct_playable: true,
    requires_proxy: true,
    proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
  }])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel())

  assert.equal(store.iptvUrls.length, 1)
  assert.equal(store.iptvUrls[0].type, 'proxy')
  assert.equal(store.iptvUrls[0].via_proxy, true)
})

test('adapter direct_playable false suppresses direct and keeps source_id proxy', async () => {
  setActivePinia(createPinia())
  installFetchStub([{
    ok: true,
    source_id: 'src_huya',
    url: 'https://tx.flv.huya.com/live/room.flv',
    source_type: 'http_flv',
    direct_playable: false,
    requires_proxy: false,
    proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
  }])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel())

  assert.equal(store.iptvUrls.length, 1)
  assert.equal(store.iptvUrls[0].type, 'proxy')
  assert.equal(store.iptvUrls[0].via_proxy, true)
  assert.match(store.iptvUrls[0].url, /source_id=src_huya/)
})

// ────────────────────────────────────────────────────────────────────
// 修复后：adapter resolve 失败 + 非 force_proxy → 保留直连+代理
// ────────────────────────────────────────────────────────────────────

test('resolve fail + non-force_proxy: 直连+代理两个选项保留', async () => {
  setActivePinia(createPinia())
  installFetchStub([new Error('resolve down')])
  const store = usePlayerStore()

  await store.playIptvChannel(adapterChannel())

  // 源菜单必须保留直连和代理两个选项
  assert.equal(store.iptvUrls.length, 2)

  // 直连 entry 保留原始 adapter identity（FullPlayer 播放时重新 resolve）
  const direct = store.iptvUrls[0]
  assert.equal(direct.type, 'direct')
  assert.notEqual(direct.via_proxy, true)
  assert.equal(direct.source_type, 'adapter')
  assert.equal(direct.adapter, 'huya')
  assert.match(direct.url, /^huya:\/\//)
  assert.match(direct.adapter_source_url, /\/api\/media\/channel\/.+\/resolve\?source_id=src_huya/)
  assert.equal(direct.adapter_volatile_url, true)

  // 代理 entry 保留 via_proxy
  const proxy = store.iptvUrls[1]
  assert.equal(proxy.via_proxy, true)
  assert.match(proxy.url, /\/api\/media\/channel\/.+\/playlist\.m3u8/)
  assert.equal(proxy.url.includes('huya://'), false)
  assert.equal(proxy.url.includes('target_url='), false)
  assert.equal(proxy.source_type, 'adapter', '首次 resolve 失败时代理的实际 transport 仍未知，不得伪装成 HLS')
  assert.equal(proxy.adapter_transport_pending, true)
})

test('resolve fail + force_proxy: 仅保留代理', async () => {
  setActivePinia(createPinia())
  installFetchStub([new Error('huya_not_live')])
  const store = usePlayerStore()
  await store.playIptvChannel(adapterChannel({ force_proxy: 1 }))

  assert.equal(store.iptvUrls.length, 1)
  assert.equal(store.iptvUrls[0].via_proxy, true)
  assert.match(store.iptvUrls[0].url, /\/api\/media\/channel\/.+\/playlist\.m3u8/)
  assert.equal(store.iptvUrls[0].source_type, 'adapter')
  assert.equal(store.iptvUrls[0].adapter_transport_pending, true)
})

test('resolve fail + non-force_proxy: 直连保留原始 adapter identity', async () => {
  setActivePinia(createPinia())
  installFetchStub([new Error('huya_not_live')])
  const store = usePlayerStore()
  await store.playIptvChannel(adapterChannel())

  assert.equal(store.iptvUrls.length, 2)
  // 直连保留原始 identity
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.equal(store.iptvUrls[0].source_type, 'adapter')
  assert.ok(store.iptvUrls[0].adapter_source_url)
  // 代理保留 via_proxy
  assert.equal(store.iptvUrls[1].via_proxy, true)
})

test('多源频道中一个 adapter resolve 失败不影响其他 source', async () => {
  setActivePinia(createPinia())
  installFetchStub([new Error('huya_not_live')])
  const store = usePlayerStore()
  const channel = {
    canonical_key: '混合频道',
    urls: [
      { url: 'huya://31421', source_id: 'src_huya', source_type: 'adapter', adapter: 'huya' },
      { url: 'https://cdn.example/live.m3u8', source_id: 'src_hls', source_type: 'hls', is_working: 1, probe_status: 'online' },
    ],
  }
  await store.playIptvChannel(channel)

  // huya 2 条 (direct+proxy) + hls 2 条 (direct+proxy) = 4
  assert.equal(store.iptvUrls.length, 4, 'huya 2 条 + hls 2 条 = 4')
  // hls 源 direct 候选存在
  assert.ok(store.iptvUrls.some((e) => e.source_id === 'src_hls' && e.type === 'direct'))
  // huya 直连 entry 保留原始 identity
  const huyaDirect = store.iptvUrls.find((e) => e.source_id === 'src_huya' && e.type === 'direct')
  assert.ok(huyaDirect, 'huya 直连 entry 必须保留')
  assert.equal(huyaDirect.source_type, 'adapter')
  assert.equal(huyaDirect.adapter_volatile_url, true)
  // huya 代理 entry 也必须存在
  assert.ok(store.iptvUrls.some((e) => e.source_id === 'src_huya' && e.via_proxy))
})

test('测速失败源保留，只有明确 disabled 的源从播放队列排除', async () => {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  const channel = {
    canonical_key: '状态测试',
    urls: [
      { url: 'https://offline.example/live.m3u8', source_id: 'src_offline', source_type: 'hls', probe_status: 'offline', is_working: 0 },
      { url: 'https://disabled.example/live.m3u8', source_id: 'src_disabled', source_type: 'hls', probe_status: 'online', is_working: 1, disabled: true },
    ],
  }

  await store.playIptvChannel(channel)

  assert.equal(store.iptvUrls.length, 2)
  assert.ok(store.iptvUrls.every((entry) => entry.source_id === 'src_offline'))
  assert.ok(store.iptvUrls.some((entry) => entry.via_proxy !== true))
  assert.ok(store.iptvUrls.some((entry) => entry.via_proxy === true))
})

test('force_proxy 或 requires_proxy_declared 的 YouTube 源禁止创建直连 iframe 候选', async () => {
  for (const proxyConstraint of [
    { force_proxy: true },
    { requires_proxy_declared: true },
  ]) {
    setActivePinia(createPinia())
    const store = usePlayerStore()
    await store.playIptvChannel({
      canonical_key: 'YouTube直播',
      urls: [{
        url: 'https://www.youtube.com/watch?v=abcdefghijk',
        source_id: 'src_youtube',
        source_type: 'youtube',
        ...proxyConstraint,
      }],
    })

    assert.equal(store.iptvUrls.some((entry) => entry.type === 'youtube'), false)
    assert.equal(store.iptvUrls.length, 1)
    assert.equal(store.iptvUrls[0].via_proxy, true)
    assert.match(store.iptvUrls[0].url, /\/api\/media\/channel\/.+\/playlist\.m3u8/)
  }
})

test('用户再次点击会重新 resolve', async () => {
  setActivePinia(createPinia())
  installFetchStub([
    new Error('huya_not_live'),
    {
      ok: true,
      source_id: 'src_huya',
      url: 'https://tx.flv.huya.com/live/room.flv',
      source_type: 'http_flv',
      direct_playable: true,
      requires_proxy: false,
      proxy_url: '/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya',
    },
  ])
  const store = usePlayerStore()

  // 第一次：resolve 失败 → 仍有直连+代理
  await store.playIptvChannel(adapterChannel())
  assert.equal(store.iptvUrls.length, 2, '第一次失败仍有 2 条')
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.equal(store.iptvUrls[0].source_type, 'adapter')
  assert.equal(store.iptvUrls[1].via_proxy, true)

  // 第二次：resolve 成功 → 直连+代理
  await store.playIptvChannel(adapterChannel())
  assert.equal(store.iptvUrls.length, 2, '第二次成功也有 2 条')
  assert.equal(store.iptvUrls[0].type, 'direct')
  assert.notEqual(store.iptvUrls[0].via_proxy, true)
  assert.equal(store.iptvUrls[1].via_proxy, true)
})
