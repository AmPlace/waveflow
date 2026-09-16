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

function createRecoveryHarness({ currentRetryLimit = 1, playDeferreds = [] } = {}) {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const recoverSource = extractBetween(
    source,
    'async function recoverIptvPlayback(',
    '\nasync function fallbackToNextIptvUrl(',
  )
  const factory = new Function('deps', `
    let _playAttemptId = 10
    let _recoverySeq = 0
    let _recoveryInFlight = false
    let _componentDisposed = false
    const {
      isIptvMode, isPlaying, playerStore, calls, playCurrentIptvUrl,
      fallbackToNextIptvUrl, markAllIptvSourcesUnavailable,
      setIptvUrlIndexForAttempt,
    } = deps
    const RECOVERY_CURRENT_RETRY_LIMIT = ${currentRetryLimit}
    const RECOVERY_CURRENT_TTL_MS = 4000
    const RECOVERY_FALLBACK_TTL_MS = 5000
    const RECOVERY_RETRY_DELAYS_MS = [0, 0]
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const isRecoveryActive = (seq) => seq === _recoverySeq && isIptvMode.value && isPlaying.value
    const clearStallRecoveryTimer = () => calls.push('stall.clear')
    const cancelCurrentStartup = () => calls.push('startup.cancel')
    const cancelActiveProxyRace = () => calls.push('race.cancel')
    const setSourceRuntimeStatus = (index, status) => calls.push('status:' + index + ':' + status)
    const jitter = () => 0
    const wait = async () => {}
    const refreshVolatileEntryForRecovery = async () => false
    const withAttemptTimeout = async (promise) => await promise
    const syncIptvMediaSession = (state) => calls.push('media:' + state)
    const markRaceLoser = () => calls.push('loser.mark')
    ${recoverSource}
    return {
      recoverIptvPlayback,
      invalidate: () => {
        _playAttemptId += 1
        _recoverySeq += 1
        _recoveryInFlight = false
      },
      snapshot: () => ({ _playAttemptId, _recoverySeq, _recoveryInFlight }),
    }
  `)

  const calls = []
  const playerStore = {
    currentIptvChannel: { canonical_key: 'old-channel' },
    iptvUrlIndex: 0,
    iptvUrls: [{ url: 'https://example.test/live.m3u8', source_id: 'source-1', type: 'direct' }],
    setLoading: (value) => calls.push(`loading:${value}`),
    setPlaybackError: (value) => calls.push(`error:${value}`),
    clearPlaybackError: () => calls.push('error:clear'),
  }
  let playIndex = 0
  const harness = factory({
    isIptvMode: { value: true },
    isPlaying: { value: true },
    playerStore,
    calls,
    playCurrentIptvUrl: async () => {
      const controlled = playDeferreds[playIndex++]
      if (controlled) return await controlled.promise
    },
    fallbackToNextIptvUrl: async () => false,
    markAllIptvSourcesUnavailable: () => calls.push('all-unavailable'),
    setIptvUrlIndexForAttempt: async () => true,
  })
  return { calls, harness, playerStore }
}

test('两个 force recovery 并发时只有最新 recovery 可以提交成功状态', async () => {
  const first = deferred()
  const second = deferred()
  const h = createRecoveryHarness({ playDeferreds: [first, second] })

  const firstRun = h.harness.recoverIptvPlayback('first', { force: true, currentRetryLimit: 1 })
  const secondRun = h.harness.recoverIptvPlayback('second', { force: true, currentRetryLimit: 1 })

  first.resolve()
  await firstRun
  assert.equal(h.calls.filter((call) => call === 'error:clear').length, 0)
  assert.equal(h.calls.filter((call) => call === 'loading:false').length, 0)

  second.resolve()
  assert.equal(await secondRun, true)
  assert.equal(h.calls.filter((call) => call === 'error:clear').length, 1)
  assert.equal(h.calls.filter((call) => call === 'loading:false').length, 1)
  assert.equal(h.calls.filter((call) => call === 'media:playing').length, 1)
})

test('recovery 延迟期间切台或进入 auth 后旧 recovery 不能回写', async () => {
  const controlled = deferred()
  const h = createRecoveryHarness({ playDeferreds: [controlled] })
  const pending = h.harness.recoverIptvPlayback('old-channel', { force: true, currentRetryLimit: 1 })

  h.harness.invalidate()
  h.playerStore.currentIptvChannel = null
  controlled.resolve()

  assert.equal(await pending, false)
  assert.equal(h.calls.filter((call) => call === 'error:clear').length, 0)
  assert.equal(h.calls.filter((call) => call === 'loading:false').length, 0)
  assert.equal(h.calls.filter((call) => call === 'all-unavailable').length, 0)
})

test('recovery 延迟期间手动切源后旧 recovery 不会改变 source index', async () => {
  const controlled = deferred()
  const h = createRecoveryHarness({ playDeferreds: [controlled] })
  const pending = h.harness.recoverIptvPlayback('old-source', { force: true, currentRetryLimit: 1 })

  h.harness.invalidate()
  h.playerStore.iptvUrlIndex = 1
  controlled.reject(new Error('old source failed'))
  await pending

  assert.equal(h.playerStore.iptvUrlIndex, 1)
  assert.equal(h.calls.filter((call) => call === 'all-unavailable').length, 0)
})

test('全部候选失败只进行一次最终结算', async () => {
  const h = createRecoveryHarness({ currentRetryLimit: 0 })

  assert.equal(await h.harness.recoverIptvPlayback('all-failed', {
    force: true,
    currentRetryLimit: 0,
  }), false)

  assert.equal(h.calls.filter((call) => call === 'all-unavailable').length, 1)
})
