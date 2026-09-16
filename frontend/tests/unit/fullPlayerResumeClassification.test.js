import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const fullPlayerPath = new URL('../../src/components/FullPlayer.vue', import.meta.url)

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

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function createHarness(error, playDeferred = null) {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const resumeSource = extractFunction(source, 'async function resumeSoftPausedIptv(')
  const factory = new Function('deps', `
    let _softResumePromise = null
    let _softPausedAt = Date.now()
    let _softPauseReleased = false
    let _playAttemptId = 1
    let _recoverySeq = 0
    let _recoveryInFlight = false
    let _componentDisposed = false
    const {
      iptvVideoRef, iptvHlsRef, iptvMpegtsRef, playerStore, calls,
    } = deps
    const PAUSE_GRACE_RELEASE_MS = 30_000
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const clearPauseReleaseTimer = () => calls.push('pause-timer.clear')
    const clearStallRecoveryTimer = () => calls.push('stall.clear')
    const attachRuntimeHlsErrorHandlers = () => calls.push('hls.handlers')
    const attachRuntimeMpegtsErrorHandlers = () => calls.push('mpegts.handlers')
    const markVideoProgress = () => calls.push('progress.mark')
    const startPlaybackProgressWatch = () => calls.push('progress.start')
    const startVideoFrameWatch = () => calls.push('frame.start')
    const setSourceRuntimeStatus = (_index, status) => calls.push('status:' + status)
    const syncIptvMediaSession = (state) => calls.push('media:' + state)
    const isAutoplayBlockedError = (value) => String(value?.name || '') === 'NotAllowedError'
    ${resumeSource}
    return {
      resumeSoftPausedIptv,
      invalidate: () => { _playAttemptId += 1 },
    }
  `)

  const calls = []
  const state = { isPlaying: true, playbackError: '' }
  const video = {
    paused: true,
    ended: false,
    src: 'https://example.test/live.m3u8',
    currentSrc: 'https://example.test/live.m3u8',
    play: async () => {
      if (playDeferred) return await playDeferred.promise
      throw error
    },
  }
  const harness = factory({
    iptvVideoRef: { value: video },
    iptvHlsRef: { value: null },
    iptvMpegtsRef: { value: null },
    playerStore: {
      iptvUrlIndex: 0,
      iptvUrls: [{ url: video.src, via_proxy: false }],
      clearPlaybackError: () => {
        state.playbackError = ''
        calls.push('error:clear')
      },
      setPlaybackError: (value) => {
        state.playbackError = value
        calls.push('error:' + value)
      },
      setLoading: (value) => calls.push('loading:' + value),
      togglePlay: (value) => {
        state.isPlaying = value
        calls.push('playing:' + value)
      },
    },
    calls,
  })
  return { calls, harness, state }
}

test('soft resume classifies only NotAllowedError as autoplay blocked', async (t) => {
  const cases = [
    {
      name: 'NotAllowedError',
      error: Object.assign(new Error('gesture required'), { name: 'NotAllowedError' }),
      expectsMessage: true,
    },
    {
      name: 'AbortError',
      error: Object.assign(new Error('play interrupted'), { name: 'AbortError' }),
      expectsMessage: false,
    },
    {
      name: 'NotSupportedError',
      error: Object.assign(new Error('unsupported media'), { name: 'NotSupportedError' }),
      expectsMessage: false,
    },
  ]

  for (const item of cases) {
    await t.test(item.name, async () => {
      const h = createHarness(item.error)
      assert.equal(await h.harness.resumeSoftPausedIptv('test'), false)
      assert.equal(h.state.playbackError.includes('浏览器阻止自动播放'), item.expectsMessage)
      assert.equal(h.state.isPlaying, item.expectsMessage ? false : true)
    })
  }
})

test('stale soft-resume rejection cannot write autoplay state for a newer attempt', async () => {
  const error = Object.assign(new Error('gesture required'), { name: 'NotAllowedError' })
  const playDeferred = deferred()
  const h = createHarness(error, playDeferred)
  const pending = h.harness.resumeSoftPausedIptv('stale')
  await Promise.resolve()
  h.harness.invalidate()
  playDeferred.reject(error)

  assert.equal(await pending, false)
  assert.equal(h.state.playbackError, '')
  assert.equal(h.state.isPlaying, true)
})
