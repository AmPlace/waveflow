import test from 'node:test'
import assert from 'node:assert/strict'

import { useEpg } from '../../src/composables/useEpg.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
  await new Promise((resolve) => setImmediate(resolve))
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

function responseJson(value) {
  return {
    ok: true,
    status: 200,
    json: async () => value,
  }
}

function programPayload(channelKey, date) {
  return {
    current: {
      title: `${channelKey} current ${date}`,
      start: `${date}T10:00:00+08:00`,
      stop: `${date}T11:00:00+08:00`,
    },
    next: {
      title: `${channelKey} next ${date}`,
      start: `${date}T11:00:00+08:00`,
      stop: `${date}T12:00:00+08:00`,
    },
    programs: [
      { title: `${channelKey} past ${date}`, start: `${date}T09:00:00+08:00`, stop: `${date}T10:00:00+08:00`, status: 'past' },
      { title: `${channelKey} current ${date}`, start: `${date}T10:00:00+08:00`, stop: `${date}T11:00:00+08:00`, status: 'current' },
      { title: `${channelKey} next ${date}`, start: `${date}T11:00:00+08:00`, stop: `${date}T12:00:00+08:00`, status: 'future' },
    ],
    date,
    available_dates: ['2026-06-18', '2026-06-19'],
  }
}

function installFetchQueue() {
  const requests = []
  const previousFetch = globalThis.fetch
  globalThis.fetch = (url, options = {}) => {
    const item = deferred()
    requests.push({ url: String(url), options, ...item })
    return item.promise
  }
  return {
    requests,
    restore() {
      globalThis.fetch = previousFetch
    },
  }
}

function createHarness() {
  const currentChannelKey = { value: '' }
  const currentDate = { value: '' }
  const playerStore = { currentEpgProgram: null }
  const epg = useEpg({
    getCurrentChannelKey: () => currentChannelKey.value,
    getCurrentDate: () => currentDate.value,
  })

  async function load(channelKey, date = '') {
    currentChannelKey.value = channelKey
    currentDate.value = date
    const result = await epg.fetchPrograms(channelKey, date ? { date } : {})
    if (result?.applied) playerStore.currentEpgProgram = epg.current.value
    return result
  }

  function clearChannel() {
    currentChannelKey.value = ''
    currentDate.value = ''
    epg.clearPrograms()
    playerStore.currentEpgProgram = null
  }

  function unmount() {
    epg.invalidatePrograms()
  }

  return { currentChannelKey, currentDate, epg, load, clearChannel, unmount, playerStore }
}

test('A 慢 B 快：旧成功不得覆盖 B 的 EPG/current/next/store', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    const b = h.load('B', '2026-06-18')
    await flush()

    fetchQueue.requests[1].resolve(responseJson(programPayload('B', '2026-06-18')))
    await b
    assert.equal(h.epg.current.value.title, 'B current 2026-06-18')
    assert.equal(h.epg.next.value.title, 'B next 2026-06-18')
    assert.equal(h.playerStore.currentEpgProgram.title, 'B current 2026-06-18')

    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    await a
    assert.equal(h.epg.current.value.title, 'B current 2026-06-18')
    assert.equal(h.epg.next.value.title, 'B next 2026-06-18')
    assert.equal(h.epg.schedule.value[1].title, 'B current 2026-06-18')
    assert.equal(h.playerStore.currentEpgProgram.title, 'B current 2026-06-18')
  } finally {
    fetchQueue.restore()
  }
})

test('A 晚失败不得清空 B 或写 error/loading/current', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    const b = h.load('B', '2026-06-18')
    await flush()

    fetchQueue.requests[1].resolve(responseJson(programPayload('B', '2026-06-18')))
    await b
    assert.equal(h.epg.loading.value, false)

    fetchQueue.requests[0].reject(new Error('A failed late'))
    await a
    assert.equal(h.epg.current.value.title, 'B current 2026-06-18')
    assert.equal(h.playerStore.currentEpgProgram.title, 'B current 2026-06-18')
    assert.equal(h.epg.error.value, '')
    assert.equal(h.epg.loading.value, false)
  } finally {
    fetchQueue.restore()
  }
})

test('同频道切日期：旧日期晚返回不得覆盖新日期', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const d1 = h.load('A', '2026-06-18')
    await flush()
    const d2 = h.load('A', '2026-06-19')
    await flush()

    fetchQueue.requests[1].resolve(responseJson(programPayload('A', '2026-06-19')))
    await d2
    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    await d1

    assert.equal(h.epg.selectedDate.value, '2026-06-19')
    assert.equal(h.epg.current.value.title, 'A current 2026-06-19')
    assert.equal(h.playerStore.currentEpgProgram.title, 'A current 2026-06-19')
  } finally {
    fetchQueue.restore()
  }
})

test('清空频道后旧请求完成不得重新写回 EPG 或 current program', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    h.clearChannel()
    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    await a

    assert.equal(h.epg.current.value, null)
    assert.equal(h.epg.next.value, null)
    assert.deepEqual(h.epg.schedule.value, [])
    assert.equal(h.playerStore.currentEpgProgram, null)
  } finally {
    fetchQueue.restore()
  }
})

test('卸载后旧请求完成不得写 refs/store', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    h.unmount()
    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    await a

    assert.equal(h.epg.current.value, null)
    assert.equal(h.epg.next.value, null)
    assert.deepEqual(h.epg.schedule.value, [])
    assert.equal(h.playerStore.currentEpgProgram, null)
  } finally {
    fetchQueue.restore()
  }
})

test('A -> B -> C 交错完成后最终只能 C 接管', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    const b = h.load('B', '2026-06-18')
    await flush()
    const c = h.load('C', '2026-06-18')
    await flush()

    fetchQueue.requests[1].resolve(responseJson(programPayload('B', '2026-06-18')))
    await b
    fetchQueue.requests[2].resolve(responseJson(programPayload('C', '2026-06-18')))
    await c
    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    await a

    assert.equal(h.epg.current.value.title, 'C current 2026-06-18')
    assert.equal(h.epg.next.value.title, 'C next 2026-06-18')
    assert.equal(h.playerStore.currentEpgProgram.title, 'C current 2026-06-18')
  } finally {
    fetchQueue.restore()
  }
})

test('loading ownership：旧 finally 不得关闭当前请求 loading', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    const b = h.load('B', '2026-06-18')
    await flush()
    assert.equal(h.epg.loading.value, true)

    fetchQueue.requests[0].reject(new Error('A failed late'))
    await a
    assert.equal(h.epg.loading.value, true)

    fetchQueue.requests[1].resolve(responseJson(programPayload('B', '2026-06-18')))
    await b
    assert.equal(h.epg.loading.value, false)
  } finally {
    fetchQueue.restore()
  }
})

test('error ownership：旧请求错误不得覆盖当前成功状态', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const a = h.load('A', '2026-06-18')
    await flush()
    const b = h.load('B', '2026-06-18')
    await flush()

    fetchQueue.requests[1].resolve(responseJson(programPayload('B', '2026-06-18')))
    await b
    fetchQueue.requests[0].reject(new Error('late A error'))
    await a

    assert.equal(h.epg.error.value, '')
    assert.equal(h.epg.current.value.title, 'B current 2026-06-18')
  } finally {
    fetchQueue.restore()
  }
})

test('正常路径回归：单频道单日期仍计算 current/next/schedule', async () => {
  const fetchQueue = installFetchQueue()
  try {
    const h = createHarness()
    const request = h.load('A', '2026-06-18')
    await flush()
    fetchQueue.requests[0].resolve(responseJson(programPayload('A', '2026-06-18')))
    const result = await request

    assert.equal(result.applied, true)
    assert.equal(h.epg.loading.value, false)
    assert.equal(h.epg.error.value, '')
    assert.equal(h.epg.current.value.title, 'A current 2026-06-18')
    assert.equal(h.epg.next.value.title, 'A next 2026-06-18')
    assert.equal(h.epg.schedule.value.length, 3)
    assert.equal(h.epg.selectedDate.value, '2026-06-18')
    assert.deepEqual(h.epg.availableDates.value, ['2026-06-18', '2026-06-19'])
    assert.equal(h.playerStore.currentEpgProgram.title, 'A current 2026-06-18')
  } finally {
    fetchQueue.restore()
  }
})
