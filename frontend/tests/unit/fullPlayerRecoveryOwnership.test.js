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

function extractFunction(source, signature) {
  const start = source.indexOf(signature)
  assert.notEqual(start, -1, `missing function: ${signature}`)
  const bodyStart = source.indexOf('{', start)
  let depth = 0
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(start, index + 1)
  }
  throw new Error(`unterminated function: ${signature}`)
}

function extractBetween(source, startSignature, endSignature) {
  const start = source.indexOf(startSignature)
  assert.notEqual(start, -1, `missing start: ${startSignature}`)
  const end = source.indexOf(endSignature, start)
  assert.notEqual(end, -1, `missing end: ${endSignature}`)
  return source.slice(start, end).trim()
}

function createTryPlayPreflightHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const helperSource = [
    'function isChannelProxyPlaylistUrl(',
    'function isKnownRtspProxyPlaylist(',
  ].map((signature) => extractFunction(source, signature)).join('\n')
  const tryPlaySource = extractBetween(
    source,
    'async function tryPlayIptv(',
    '\nfunction pruneRaceLosers(',
  )
  const factory = new Function('deps', `
    let _playAttemptId = 1
    let _componentDisposed = false
    let _softPausedAt = 0
    let _softPauseReleased = false
    let _cancelCurrentStartup = null
    const {
      iptvVideoRef, activeIptvEngine, playerStore, preflightProxyPlaylistTransport,
      setSourceRuntimeStatus, setSourceRuntimeStatusByUrl, canUseHls,
    } = deps
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const cancelledError = () => new Error('cancelled')
    const clearPauseReleaseTimer = () => {}
    const cancelCurrentStartup = () => {
      if (!_cancelCurrentStartup) return
      const cancel = _cancelCurrentStartup
      _cancelCurrentStartup = null
      cancel()
    }
    const cancelActiveProxyRace = () => {}
    const destroyIptvEngines = () => {}
    const stopPlaybackWatchdogs = () => {}
    const resetMediaAspect = () => {}
    const resetIptvVideo = () => {}
    const playbackEngineType = (type) => type || 'hls'
    const sourceType = () => 'hls'
    const isHlsUrl = () => true
    const isMpegTsEngineType = () => false
    const isMpegTsUrl = () => false
    const window = { location: { origin: 'http://localhost' } }
    const canUseMpegTs = () => false
    const updateMediaAspectFromVideo = () => {}
    const attachRuntimeHlsErrorHandlers = () => {}
    const attachRuntimeMpegtsErrorHandlers = () => {}
    const releaseTrackedHls = () => {}
    const clearCurrentHlsIf = () => {}
    const clearCurrentMpegtsIf = () => {}
    const trackHlsSource = () => {}
    const isIOS = false
    const volume = { value: 1 }
    ${helperSource}
    ${tryPlaySource}
    return {
      cancelStartup: cancelCurrentStartup,
      tryPlayIptv,
      setAttempt: (value) => { _playAttemptId = value },
    }
  `)

  const statuses = []
  let canUseHlsCalls = 0
  let preflightSignal = null
  const preflight = deferred()
  const video = {
    currentTime: 0,
    canPlayType: () => '',
    addEventListener() {},
    removeEventListener() {},
  }
  const harness = factory({
    iptvVideoRef: { value: video },
    activeIptvEngine: { value: 'video' },
    playerStore: {
      setLoading(value) { statuses.push(`loading:${value}`) },
      togglePlay(value) { statuses.push(`playing:${value}`) },
    },
    preflightProxyPlaylistTransport: (_url, _usingProxy, signal) => {
      preflightSignal = signal
      signal?.addEventListener('abort', () => preflight.resolve(null), { once: true })
      return preflight.promise
    },
    setSourceRuntimeStatus: (_index, status) => statuses.push(status),
    setSourceRuntimeStatusByUrl: (_url, status) => statuses.push(status),
    canUseHls: () => {
      canUseHlsCalls += 1
      return false
    },
  })
  return {
    canUseHlsCalls: () => canUseHlsCalls,
    harness,
    preflight,
    preflightSignal: () => preflightSignal,
    statuses,
  }
}

function createSoftRecoveryHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const doRecoverySource = extractFunction(source, 'async function doRecovery(')
  const factory = new Function('deps', `
    let _stallRecovering = false
    let _lastRecoveryTime = 0
    let _lastVideoProgressAt = 0
    let _playAttemptId = 1
    let _componentDisposed = false
    let _stallRecoverySeq = 0
    const { iptvVideoRef, iptvHlsRef, seekNearLiveEdge } = deps
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    ${doRecoverySource}
    return {
      doRecovery,
      setAttempt: (value) => { _playAttemptId = value },
      snapshot: () => ({ _stallRecovering }),
    }
  `)

  const playDeferred = deferred()
  let seekCount = 0
  const oldVideo = {
    paused: false,
    currentTime: 0,
    readyState: 2,
    networkState: 2,
    play: () => playDeferred.promise,
  }
  const newVideo = {
    paused: false,
    currentTime: 100,
    readyState: 4,
    networkState: 1,
    play: async () => {},
  }
  const iptvVideoRef = { value: oldVideo }
  const harness = factory({
    iptvVideoRef,
    iptvHlsRef: { value: { startLoad() {} } },
    seekNearLiveEdge: () => {
      seekCount += 1
      oldVideo.currentTime = 18
      return true
    },
  })
  return { harness, iptvVideoRef, newVideo, oldVideo, playDeferred, seekCount: () => seekCount }
}

function createVideoEventHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const onVideoEventSource = extractFunction(source, 'function onVideoEvent(')
  const onVideoStalledSource = extractFunction(source, 'function onVideoStalled()')
  const factory = new Function('deps', `
    let _cancelCurrentStartup = () => {}
    let _softPausedAt = 123
    let _softPauseReleased = true
    const {
      iptvVideoRef, playerStore, calls, clearPauseReleaseTimer,
      markVideoProgress, startPlaybackProgressWatch, startVideoFrameWatch,
      clearStallRecoveryTimer, syncIptvMediaSession, stopPlaybackProgressWatch,
      stopVideoFrameWatch, scheduleStallRecovery,
    } = deps
    ${onVideoStalledSource}
    ${onVideoEventSource}
    return {
      onVideoStalled,
      onVideoEvent,
      finishStartup: () => { _cancelCurrentStartup = null },
    }
  `)

  const calls = []
  const video = { paused: false }
  const harness = factory({
    iptvVideoRef: { value: video },
    playerStore: {
      setLoading: (value) => calls.push(`loading:${value}`),
      togglePlay: (value) => calls.push(`playing:${value}`),
    },
    calls,
    clearPauseReleaseTimer: () => calls.push('pause-timer.clear'),
    markVideoProgress: () => calls.push('progress.mark'),
    startPlaybackProgressWatch: () => calls.push('progress.start'),
    startVideoFrameWatch: () => calls.push('frame.start'),
    clearStallRecoveryTimer: () => calls.push('stall.clear'),
    syncIptvMediaSession: (state) => calls.push(`media:${state}`),
    stopPlaybackProgressWatch: () => calls.push('progress.stop'),
    stopVideoFrameWatch: () => calls.push('frame.stop'),
    scheduleStallRecovery: (reason) => calls.push(`recovery:${reason}`),
  })
  return { calls, harness }
}

test('旧 proxy transport preflight 晚到后不得继续创建播放管线或写失败状态', async () => {
  const h = createTryPlayPreflightHarness()
  const pending = h.harness.tryPlayIptv(
    '/api/media/channel/example/playlist.m3u8',
    true,
    '',
    1,
    0,
    'hls',
  )

  h.harness.setAttempt(2)
  h.harness.cancelStartup()

  await assert.rejects(pending, /cancelled/)
  assert.equal(h.preflightSignal()?.aborted, true)
  assert.equal(h.canUseHlsCalls(), 0)
  assert.deepEqual(h.statuses, ['trying', 'loading:true'])
})

test('显式 cancel startup 即使尚未递增 attempt 也必须终止 preflight 后续流程', async () => {
  const h = createTryPlayPreflightHarness()
  const pending = h.harness.tryPlayIptv(
    '/api/media/channel/example/playlist.m3u8',
    true,
    '',
    1,
    0,
    'hls',
  )

  h.harness.cancelStartup()

  await assert.rejects(pending, /cancelled/)
  assert.equal(h.preflightSignal()?.aborted, true)
  assert.equal(h.canUseHlsCalls(), 0)
  assert.deepEqual(h.statuses, ['trying', 'loading:true'])
})

test('soft recovery 的 play Promise 晚到后不得 seek 已失去所有权的旧 video', async () => {
  const h = createSoftRecoveryHarness()
  const pending = h.harness.doRecovery(h.oldVideo, 'controlled-stall')

  h.harness.setAttempt(2)
  h.iptvVideoRef.value = h.newVideo
  h.playDeferred.resolve()
  await pending

  assert.equal(h.seekCount(), 0)
  assert.equal(h.oldVideo.currentTime, 0)
  assert.equal(h.harness.snapshot()._stallRecovering, false)
})

test('新 source 正在 startup 时忽略旧 video playing/waiting/pause 事件', () => {
  const h = createVideoEventHarness()

  h.harness.onVideoEvent('playing')
  h.harness.onVideoEvent('waiting')
  h.harness.onVideoEvent('pause')
  h.harness.onVideoStalled()

  assert.deepEqual(h.calls, [])
})

test('用户主动 pause 只停止 watchdog 和 MediaSession，不触发 recovery', () => {
  const h = createVideoEventHarness()
  h.harness.finishStartup()

  h.harness.onVideoEvent('pause')

  assert.deepEqual(h.calls, [
    'stall.clear',
    'progress.stop',
    'frame.stop',
    'media:paused',
  ])
  assert.equal(h.calls.some((call) => call.startsWith('recovery:')), false)
})
