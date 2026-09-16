import test from 'node:test'
import assert from 'node:assert/strict'

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
  await new Promise((resolve) => setImmediate(resolve))
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

function responseJson(value) {
  return { ok: true, status: 200, json: async () => value }
}

function channelPayload(channelNames, group = '测试') {
  return {
    channels: channelNames.map((name) => ({
      canonical_key: name.toLowerCase().replace(/\s+/g, '-'),
      name,
      group_name: group,
      urls: [{ url: 'https://cdn.example/live.m3u8', source_id: `src_${name}`, source_type: 'hls' }],
      tvg_id_candidates: [],
      normalized_candidates: [],
    })),
    groups: [group],
    adapter_capabilities: {},
    total: channelNames.length,
  }
}

function installFetchQueue() {
  const requests = []
  const prev = globalThis.fetch
  globalThis.fetch = (url, options = {}) => {
    const item = deferred()
    requests.push({ url: String(url), options, ...item })
    return item.promise
  }
  return { requests, restore() { globalThis.fetch = prev } }
}

function createLoadChannelsHarness() {
  const state = {
    allChannels: [],
    allGroups: [],
    adapterCoverSupported: new Set(),
    epgMap: {},
    loading: false,
    selectedGroup: '',
    searchQuery: '',
  }
  const batchEpgCalls = []
  const batchEpg = async (keys) => {
    const d = deferred()
    batchEpgCalls.push({ keys, ...d })
    return d.promise
  }

  let requestSeq = 0
  let activeRequestSeq = 0
  let activeController = null

  function invalidateListRequest() {
    requestSeq += 1
    activeRequestSeq = 0
    if (activeController) { activeController.abort(); activeController = null }
  }

  function isCurrentListRequest(seq) {
    return seq === activeRequestSeq && seq === requestSeq
  }

  async function loadChannels() {
    invalidateListRequest()
    const seq = ++requestSeq
    activeRequestSeq = seq
    const ctrl = { abort() { this.aborted = true }, aborted: false }
    activeController = ctrl

    state.loading = true
    try {
      const group = state.selectedGroup
      const search = state.searchQuery.trim()
      const res = await fetch(`/api/iptv/channels?group=${group}&search=${search}`, { signal: ctrl.signal })
      const data = await res.json()
      if (!isCurrentListRequest(seq)) return { applied: false }
      state.allChannels = data.channels || []
      if (!group && !search) state.allGroups = data.groups || []
      const caps = data.adapter_capabilities || {}
      const next = new Set()
      for (const [name, list] of Object.entries(caps)) {
        if (Array.isArray(list) && list.includes('cover')) next.add(String(name).toLowerCase())
      }
      if (isCurrentListRequest(seq)) state.adapterCoverSupported = next
      const keys = (data.channels || []).map((c) => c.canonical_key).filter(Boolean)
      if (keys.length && isCurrentListRequest(seq)) {
        const batchSeq = seq
        batchEpg(keys).then((m) => {
          if (isCurrentListRequest(batchSeq)) state.epgMap = m || {}
        })
      }
      return { applied: true }
    } catch (e) {
      if (!isCurrentListRequest(seq)) return { applied: false }
      if (e?.name === 'AbortError') return { applied: false }
      return { applied: false }
    } finally {
      if (isCurrentListRequest(seq)) state.loading = false
    }
  }

  function unmount() { invalidateListRequest() }
  return { state, loadChannels, unmount, batchEpgCalls }
}

test('搜索 A 慢 AB 快：最终只显示 AB', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'
    const a = h.loadChannels()
    await flush()
    h.state.searchQuery = 'AB'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['AB-1', 'AB-2'])))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['AB-1', 'AB-2'])
    assert.equal(h.state.loading, false)
    fetchQ.requests[0].resolve(responseJson(channelPayload(['A-1'])))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['AB-1', 'AB-2'])
    assert.equal(h.state.loading, false)
  } finally { fetchQ.restore() }
})

test('旧 A 晚失败不得清空 AB 或写 error', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'
    const a = h.loadChannels()
    await flush()
    h.state.searchQuery = 'AB'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['AB-1'])))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['AB-1'])
    fetchQ.requests[0].reject(new Error('A failed late'))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['AB-1'])
    assert.equal(h.state.loading, false)
  } finally { fetchQ.restore() }
})

test('分类 1 慢 分类 2 快：最终只显示分类 2', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.selectedGroup = '央视'
    const a = h.loadChannels()
    await flush()
    h.state.selectedGroup = '卫视'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['卫视-1'], '卫视')))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['卫视-1'])
    fetchQ.requests[0].resolve(responseJson(channelPayload(['央视-1'], '央视')))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['卫视-1'])
  } finally { fetchQ.restore() }
})

test('搜索词和分类同时变化：最终只应用最后一组条件', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'CCTV'; h.state.selectedGroup = '央视'
    const a = h.loadChannels()
    await flush()
    h.state.searchQuery = '浙江'; h.state.selectedGroup = '卫视'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['浙江卫视'], '卫视')))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['浙江卫视'])
    fetchQ.requests[0].resolve(responseJson(channelPayload(['CCTV-1'], '央视')))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['浙江卫视'])
  } finally { fetchQ.restore() }
})

test('旧请求 finally 不得修改新请求 loading', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'
    const a = h.loadChannels()
    await flush()
    h.state.searchQuery = 'B'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['B-1'])))
    await b
    assert.equal(h.state.loading, false)
    fetchQ.requests[0].reject(new Error('A timeout'))
    await a
    assert.equal(h.state.loading, false)
  } finally { fetchQ.restore() }
})

test('旧 batch EPG 不得写入新列表 epgMap', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'
    const a = h.loadChannels()
    await flush()
    fetchQ.requests[0].resolve(responseJson(channelPayload(['A-1'])))
    await a
    await flush()
    const oldBatch = h.batchEpgCalls[0]

    h.state.searchQuery = 'B'
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['B-1'])))
    await b
    await flush()
    const newBatch = h.batchEpgCalls[1]

    newBatch.resolve({ 'b-1': { current: { title: 'B show' } } })
    await flush()
    assert.deepEqual(h.state.epgMap, { 'b-1': { current: { title: 'B show' } } })

    oldBatch.resolve({ 'a-1': { current: { title: 'A show' } } })
    await flush()
    assert.deepEqual(h.state.epgMap, { 'b-1': { current: { title: 'B show' } } })
  } finally { fetchQ.restore() }
})

test('组件卸载后旧请求不得写状态', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'
    const a = h.loadChannels()
    await flush()
    h.unmount()
    fetchQ.requests[0].resolve(responseJson(channelPayload(['A-1'])))
    await a
    assert.deepEqual(h.state.allChannels, [])
  } finally { fetchQ.restore() }
})

test('A→B→C 完成顺序 1：B 先回 → C 回 → A 回，最终 C', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'; const a = h.loadChannels(); await flush()
    h.state.searchQuery = 'B'; const b = h.loadChannels(); await flush()
    h.state.searchQuery = 'C'; const c = h.loadChannels(); await flush()

    // B returns (but C already invalidated B)
    fetchQ.requests[1].resolve(responseJson(channelPayload(['B-1'])))
    await b
    // B is stale → state still empty
    assert.deepEqual(h.state.allChannels, [])

    // C returns
    fetchQ.requests[2].resolve(responseJson(channelPayload(['C-1'])))
    await c
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['C-1'])

    // A returns (stale)
    fetchQ.requests[0].resolve(responseJson(channelPayload(['A-1'])))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['C-1'])
  } finally { fetchQ.restore() }
})

test('A→B→C 完成顺序 2：C 先回 → A 回 → B 回，最终 C', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'A'; const a = h.loadChannels(); await flush()
    h.state.searchQuery = 'B'; const b = h.loadChannels(); await flush()
    h.state.searchQuery = 'C'; const c = h.loadChannels(); await flush()

    fetchQ.requests[2].resolve(responseJson(channelPayload(['C-1'])))
    await c
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['C-1'])

    fetchQ.requests[0].resolve(responseJson(channelPayload(['A-1'])))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['C-1'])

    fetchQ.requests[1].resolve(responseJson(channelPayload(['B-1'])))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['C-1'])
  } finally { fetchQ.restore() }
})

test('正常单次列表加载行为不变', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = ''; h.state.selectedGroup = ''
    const result = h.loadChannels()
    await flush()
    fetchQ.requests[0].resolve(responseJson(channelPayload(['CCTV-1', 'CCTV-2'])))
    const r = await result
    assert.equal(r.applied, true)
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['CCTV-1', 'CCTV-2'])
    assert.deepEqual(h.state.allGroups, ['测试'])
    assert.equal(h.state.loading, false)
  } finally { fetchQ.restore() }
})

test('相同条件主动刷新仍能正常重新请求', async () => {
  const fetchQ = installFetchQueue()
  try {
    const h = createLoadChannelsHarness()
    h.state.searchQuery = 'CCTV'
    const a = h.loadChannels()
    await flush()
    fetchQ.requests[0].resolve(responseJson(channelPayload(['CCTV-1'])))
    await a
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['CCTV-1'])
    const b = h.loadChannels()
    await flush()
    fetchQ.requests[1].resolve(responseJson(channelPayload(['CCTV-1', 'CCTV-2'])))
    await b
    assert.deepEqual(h.state.allChannels.map((c) => c.name), ['CCTV-1', 'CCTV-2'])
  } finally { fetchQ.restore() }
})
