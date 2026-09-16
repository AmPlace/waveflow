import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const fullPlayerPath = new URL('../../src/components/FullPlayer.vue', import.meta.url)

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function extractBetween(source, startSignature, endSignature) {
  const start = source.indexOf(startSignature)
  assert.notEqual(start, -1, `missing start: ${startSignature}`)
  const end = source.indexOf(endSignature, start)
  assert.notEqual(end, -1, `missing end: ${endSignature}`)
  return source.slice(start, end).trim()
}

function createVolatileHarness(resolves, failureMessage = 'expired') {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const playSource = extractBetween(
    source,
    'async function playCurrentIptvUrl(',
    '\nfunction probeBufferAhead(',
  )
  const factory = new Function('deps', `
    let _playAttemptId = 1
    let _componentDisposed = false
    const { playerStore, iptvVideoRef, calls, tryPlayIptv } = deps
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const preflightYoutubeApiForQueue = () => {}
    const sourceType = () => 'hls'
    const markAllIptvSourcesUnavailable = () => calls.push('all-unavailable')
    const collectDirectRacers = () => []
    const collectProxyRacers = () => []
    const runHedgedRace = async () => false
    const setSourceRuntimeStatus = (index, status) => calls.push('status:' + index + ':' + status)
    const startYoutubeCandidate = async () => {}
    const isProxyLikeEntry = () => false
    const clearRaceLoser = () => {}
    const isAutoplayBlockedError = () => false
    const isMpegTsEngineType = () => false
    const recordMpegtsReconnect = () => 99
    const MPEGTS_RECONNECT_LIMIT = 0
    const MPEGTS_ERROR_RECONNECT_DELAY_MS = 0
    const fallbackToNextIptvUrl = async () => false
    ${playSource}
    return {
      playCurrentIptvUrl,
      setAttempt: (value) => { _playAttemptId = value },
      currentAttempt: () => _playAttemptId,
    }
  `)

  const calls = []
  let resolveIndex = 0
  const entry = {
    url: 'https://expired/live.m3u8',
    type: 'direct',
    source_type: 'hls',
    adapter_volatile_url: true,
    adapter_source_url: 'huya://room',
  }
  const playerStore = {
    currentIptvChannel: { canonical_key: 'adapter-channel' },
    iptvUrls: [entry],
    iptvUrlIndex: 0,
    clearPlaybackError: () => calls.push('error:clear'),
    setPlaybackError: (value) => calls.push(`error:${value}`),
    setLoading: (value) => calls.push(`loading:${value}`),
    togglePlay: (value) => calls.push(`playing:${value}`),
    reResolveAdapterUrl: async () => await resolves[resolveIndex++].promise,
  }
  const harness = factory({
    playerStore,
    iptvVideoRef: { value: {} },
    calls,
    tryPlayIptv: async (url) => {
      if (url === 'https://fresh-2/live.m3u8') return
      if (url === 'https://fresh/live.m3u8') return
      throw new Error(failureMessage)
    },
  })
  return { calls, entry, harness }
}

test('旧 Adapter resolve 晚成功不能覆盖新 attempt 已写入的 URL 和 source_type', async () => {
  const first = deferred()
  const second = deferred()
  const h = createVolatileHarness([first, second])

  const oldRun = h.harness.playCurrentIptvUrl(1, { allowStartupRace: false })
  await Promise.resolve()
  h.harness.setAttempt(2)
  const newRun = h.harness.playCurrentIptvUrl(2, { allowStartupRace: false })

  second.resolve({ url: 'https://fresh-2/live.m3u8', source_type: 'hls' })
  await newRun
  assert.equal(h.entry.url, 'https://fresh-2/live.m3u8')
  assert.equal(h.entry.source_type, 'hls')

  first.resolve({ url: 'https://stale-1/live.flv', source_type: 'http_flv' })
  await oldRun
  assert.equal(h.entry.url, 'https://fresh-2/live.m3u8')
  assert.equal(h.entry.source_type, 'hls')
  assert.equal(h.harness.currentAttempt(), 3)
})

test('adapter child playlist 过期时先重新 resolve 当前源再继续播放', async () => {
  const refresh = deferred()
  const h = createVolatileHarness([refresh], 'child playlist HTTP 401')

  const play = h.harness.playCurrentIptvUrl(1, { allowStartupRace: false })
  await Promise.resolve()
  refresh.resolve({ url: 'https://fresh/live.m3u8', source_type: 'hls' })
  await play

  assert.equal(h.entry.url, 'https://fresh/live.m3u8')
  assert.equal(h.entry.source_type, 'hls')
  assert.equal(h.entry._volatileRetryCount, 1)
  assert.ok(h.calls.includes('error:直连失败，正在获取新地址...'))
})
