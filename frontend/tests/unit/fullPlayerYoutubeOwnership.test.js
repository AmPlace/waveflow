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

function extractBetween(source, startSignature, endSignature) {
  const start = source.indexOf(startSignature)
  assert.notEqual(start, -1, `missing start: ${startSignature}`)
  const end = source.indexOf(endSignature, start)
  assert.notEqual(end, -1, `missing end: ${endSignature}`)
  return source.slice(start, end).trim()
}

function extractWatcherCallback(source, signature) {
  const start = source.indexOf(signature)
  assert.notEqual(start, -1, `missing watcher: ${signature}`)
  const bodyStart = source.indexOf('{', start)
  let depth = 0
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(bodyStart, index + 1)
  }
  throw new Error(`unterminated watcher: ${signature}`)
}

function createYoutubeHarness({ videoId = 'video-1', liveEmbedUrl = '' } = {}) {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const clearTimerSource = extractFunction(source, 'function clearYoutubeStartupTimer()')
  const destroySource = extractFunction(source, 'function destroyYoutubePlayer(')
  const syncAudioSource = extractFunction(source, 'function syncYoutubeAudioState()')
  const autoplayErrorSource = extractFunction(source, 'function isAutoplayBlockedError(')
  const youtubeErrorSource = extractFunction(source, 'function youtubePlaybackErrorMessage(')
  const youtubeEmbedSource = extractFunction(source, 'function isAllowedYoutubeEmbedUrl(')
  const youtubeHostSource = extractFunction(source, 'function isYoutubeInputHost(')
  const youtubeHttpSource = extractFunction(source, 'function isValidYoutubeHttpUrl(')
  const youtubePartsSource = extractFunction(source, 'function youtubeUrlParts(')
  const youtubeVideoIdSource = extractFunction(source, 'function parseYoutubeVideoId(')
  const youtubeSource = extractBetween(
    source,
    'async function handleActiveYoutubeFailure(',
    '\nfunction canUseHls(',
  )
  const factory = new Function('deps', `
    let _playAttemptId = 1
    let _componentDisposed = false
    let _youtubePlayer = null
    let _youtubeStartupTimer = null
    let _cancelCurrentStartup = null
    const { calls, timers, window, document, youtubeHostRef, activeIptvEngine } = deps
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const cancelledError = () => new Error('cancelled')
    const setSourceRuntimeStatus = (index, status) => calls.push('status:' + index + ':' + status)
    const setSourceRuntimeStatusByEntry = (_entry, status) => calls.push('status:entry:' + status)
    const playerStore = {
      setLoading: (value) => calls.push('loading:' + value),
      togglePlay: (value) => calls.push('playing:' + value),
      clearPlaybackError: () => calls.push('error:clear'),
    }
    const cancelCurrentStartup = () => {
      if (!_cancelCurrentStartup) return
      const cancel = _cancelCurrentStartup
      _cancelCurrentStartup = null
      cancel()
    }
    const cancelActiveProxyRace = () => {}
    const destroyIptvHls = () => {}
    const destroyIptvMpegts = () => {}
    const resetIptvVideo = () => {}
    const resetMediaAspect = () => {}
    const youtubeVideoId = () => deps.videoId
    const youtubeLiveEmbedUrl = () => deps.liveEmbedUrl
    const loadYoutubeIframeApi = async () => true
    const syncIptvMediaSession = (state) => calls.push('media:' + state)
    const fallbackToNextIptvUrl = async () => { calls.push('fallback'); return true }
    const playCurrentIptvUrl = async () => { calls.push('play-next') }
    const markAllIptvSourcesUnavailable = () => calls.push('all-unavailable')
    const volume = { value: 1 }
    const iptvMuted = { value: false }
    const setTimeout = (callback, delay) => {
      const timer = { callback, delay }
      timers.add(timer)
      return timer
    }
    const clearTimeout = (timer) => timers.delete(timer)
    ${clearTimerSource}
    ${destroySource}
    ${syncAudioSource}
    ${autoplayErrorSource}
    ${youtubeErrorSource}
    ${youtubeEmbedSource}
    ${youtubeHostSource}
    ${youtubeHttpSource}
    ${youtubePartsSource}
    ${youtubeVideoIdSource}
    ${youtubeSource}
    return {
      startYoutubeCandidate,
      isAllowedYoutubeEmbedUrl,
      isAutoplayBlockedError,
      youtubePlaybackErrorMessage,
      parseYoutubeVideoId,
      setAttempt: (value) => { _playAttemptId = value },
    }
  `)

  const calls = []
  const timers = new Set()
  const eventSets = []
  let playerTarget = null
  const player = {
    playVideo: () => calls.push('youtube.play'),
    pauseVideo: () => calls.push('youtube.pause'),
    stopVideo: () => calls.push('youtube.stop'),
    destroy: () => calls.push('youtube.destroy'),
    setVolume: () => {},
    unMute: () => {},
  }
  const window = {
    YT: {
      Player: function Player(_host, options) {
        playerTarget = _host
        eventSets.push(options.events)
        return player
      },
      PlayerState: { PLAYING: 1, PAUSED: 2, BUFFERING: 3, ENDED: 0 },
    },
  }
  const harness = factory({
    calls,
    timers,
    window,
    document: {
      createElement: () => ({ style: {}, remove() {} }),
    },
    videoId,
    liveEmbedUrl,
    youtubeHostRef: { value: { innerHTML: '', appendChild: (node) => calls.push(`host.append:${node.src}`) } },
    activeIptvEngine: { value: 'video' },
  })
  return {
    calls,
    events: () => eventSets[eventSets.length - 1] || null,
    eventSets,
    harness,
    playerTarget: () => playerTarget,
    timers,
  }
}

test('旧 YouTube callback 晚到后不能暂停、报错或 fallback 新频道', async () => {
  const h = createYoutubeHarness()
  const pending = h.harness.startYoutubeCandidate({ url: 'youtube://video-1' }, 1, 0)
  await Promise.resolve()
  await Promise.resolve()
  const events = h.events()
  assert.ok(events)

  events.onReady()
  events.onStateChange({ data: 1 })
  await pending
  assert.equal(h.timers.size, 0)

  h.calls.length = 0
  h.harness.setAttempt(2)
  events.onStateChange({ data: 2 })
  events.onStateChange({ data: 0 })
  events.onError({ data: 500 })

  assert.deepEqual(h.calls, [])
  assert.equal(h.timers.size, 0)
})

test('旧 YouTube onReady 晚到不得销毁新 player 或清掉新 startup', async () => {
  const h = createYoutubeHarness()
  const oldPending = h.harness.startYoutubeCandidate({ url: 'youtube://video-old' }, 1, 0)
  oldPending.catch(() => {})
  await Promise.resolve()
  await Promise.resolve()
  const oldEvents = h.eventSets[0]
  assert.ok(oldEvents)

  h.harness.setAttempt(2)
  const newPending = h.harness.startYoutubeCandidate({ url: 'youtube://video-new' }, 2, 1)
  await Promise.resolve()
  await Promise.resolve()
  const newEvents = h.eventSets[1]
  assert.ok(newEvents)
  newEvents.onReady()
  newEvents.onStateChange({ data: 1 })
  await newPending

  h.calls.length = 0
  oldEvents.onReady()
  await Promise.resolve()
  assert.equal(h.calls.includes('youtube.destroy'), false)
  assert.equal(h.calls.includes('youtube.stop'), false)
})

test('当前 YouTube error 会销毁当前实例并进入下一备用源', async () => {
  const h = createYoutubeHarness()
  const pending = h.harness.startYoutubeCandidate({ url: 'youtube://video-1' }, 1, 0)
  await Promise.resolve()
  await Promise.resolve()
  const events = h.events()
  events.onReady()
  events.onStateChange({ data: 1 })
  await pending

  h.calls.length = 0
  events.onError({ data: 500 })
  await Promise.resolve()
  await Promise.resolve()
  assert.ok(h.calls.includes('youtube.destroy'))
  assert.ok(h.calls.includes('fallback'))
  assert.ok(h.calls.includes('play-next'))
})

test('YouTube BUFFERING 不得提前确认播放，PLAYING 才完成起播', async () => {
  const h = createYoutubeHarness()
  const pending = h.harness.startYoutubeCandidate({ url: 'youtube://video-1' }, 1, 0)
  await Promise.resolve()
  await Promise.resolve()
  const events = h.events()
  events.onReady()
  events.onStateChange({ data: 3 })

  assert.equal(h.calls.includes('playing:true'), false)
  assert.equal(h.calls.includes('loading:false'), false)
  assert.equal(h.timers.size, 1)

  events.onStateChange({ data: 1 })
  await pending
  assert.equal(h.calls.includes('playing:true'), true)
  assert.equal(h.timers.size, 0)
})

test('YouTube error code 映射为可解释的播放失败类别', () => {
  const h = createYoutubeHarness()
  assert.equal(h.harness.youtubePlaybackErrorMessage(2), 'YouTube 视频参数无效')
  assert.equal(h.harness.youtubePlaybackErrorMessage(100), 'YouTube 视频不可用')
  assert.equal(h.harness.youtubePlaybackErrorMessage(101), 'YouTube 视频不允许嵌入')
  assert.equal(h.harness.youtubePlaybackErrorMessage(150), 'YouTube 视频不允许嵌入')
  assert.equal(h.harness.youtubePlaybackErrorMessage(5), 'YouTube 播放器不支持此视频')
  assert.equal(h.harness.youtubePlaybackErrorMessage(999), 'YouTube 播放失败')
})

test('外部 live embed URL 必须是 HTTPS YouTube embed authority', async () => {
  const h = createYoutubeHarness({
    videoId: '',
    liveEmbedUrl: 'https://evil.example/embed/live_stream?channel=UC12345678901234567890',
  })
  assert.equal(h.harness.isAllowedYoutubeEmbedUrl('https://www.youtube.com/embed/live_stream'), true)
  assert.equal(h.harness.isAllowedYoutubeEmbedUrl('http://www.youtube.com/embed/live_stream'), false)
  assert.equal(h.harness.isAllowedYoutubeEmbedUrl('https://www.youtube.com/watch?v=abcDEF123_4'), false)
  assert.equal(h.harness.isAllowedYoutubeEmbedUrl('https://evil.youtube.com/embed/live_stream'), false)
  await assert.rejects(
    h.harness.startYoutubeCandidate({ url: 'youtube://channel/UC12345678901234567890/live' }, 1, 0),
    /YouTube embed URL 不受支持/,
  )
  assert.equal(h.eventSets.length, 0)
})

test('自动播放错误只识别 NotAllowedError', () => {
  const h = createYoutubeHarness()
  assert.equal(h.harness.isAutoplayBlockedError({ name: 'NotAllowedError' }), true)
  assert.equal(h.harness.isAutoplayBlockedError({ name: 'AbortError', message: 'play() failed' }), false)
  assert.equal(h.harness.isAutoplayBlockedError({ name: 'NotSupportedError' }), false)
  assert.equal(h.harness.isAutoplayBlockedError(new Error('notallowed')), false)
})

test('YouTube video parser rejects playlist, non-HTTPS, and credentialed URLs', () => {
  const h = createYoutubeHarness()
  assert.equal(h.harness.parseYoutubeVideoId('https://www.youtube.com/watch?v=abcDEF123_4'), 'abcDEF123_4')
  assert.equal(h.harness.parseYoutubeVideoId('https://www.youtube.com/playlist?v=abcDEF123_4'), '')
  assert.equal(h.harness.parseYoutubeVideoId('http://www.youtube.com/watch?v=abcDEF123_4'), '')
  assert.equal(h.harness.parseYoutubeVideoId('https://user@www.youtube.com/watch?v=abcDEF123_4'), '')
  assert.equal(h.harness.parseYoutubeVideoId('http://youtu.be/abcDEF123_4'), '')
})

test('应用播放状态切换会控制当前 YouTube Player 暂停和恢复', () => {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const watcherBody = extractWatcherCallback(source, 'watch(isPlaying, (playing) => {')
  const calls = []
  const factory = new Function('deps', `
    const { calls } = deps
    const isIptvMode = { value: true }
    const activeIptvEngine = { value: 'youtube' }
    const _youtubePlayer = {
      playVideo: () => calls.push('youtube.play'),
      pauseVideo: () => calls.push('youtube.pause'),
    }
    const syncIptvMediaSession = (state) => calls.push('media:' + state)
    const iptvVideoRef = { value: null }
    const resumeSoftPausedIptv = async () => false
    const recoverIptvPlayback = async () => false
    const pauseIptvPlaybackPreservingFrame = () => {}
    const onPlayingChange = (playing) => ${watcherBody}
    return { onPlayingChange }
  `)
  const harness = factory({ calls })

  harness.onPlayingChange(false)
  harness.onPlayingChange(true)

  assert.deepEqual(calls, [
    'youtube.pause',
    'media:paused',
    'youtube.play',
    'media:playing',
  ])
})

test('仅 channel_id 的 YouTube 直播也必须交给可控 Player API', async () => {
  const h = createYoutubeHarness({
    videoId: '',
    liveEmbedUrl: 'https://www.youtube.com/embed/live_stream?channel=UC12345678901234567890',
  })
  const pending = h.harness.startYoutubeCandidate({
    url: 'youtube://channel/UC12345678901234567890/live',
    youtube_channel_id: 'UC12345678901234567890',
  }, 1, 0)
  await Promise.resolve()
  await Promise.resolve()

  const events = h.events()
  assert.ok(events, 'channel live iframe 必须由 YT.Player 接管')
  assert.ok(h.playerTarget(), 'YT.Player 必须绑定实际 iframe/host')
  events.onReady()
  events.onStateChange({ data: 1 })
  await pending
  assert.equal(h.timers.size, 0)
})
