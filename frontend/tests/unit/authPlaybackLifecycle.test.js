import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { createPinia, setActivePinia } from 'pinia'

import { usePlayerStore } from '../../src/stores/player.js'

const appPath = new URL('../../src/App.vue', import.meta.url)
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

function createFullPlayerDisposeHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const isAttemptActiveSource = extractFunction(source, 'function isAttemptActive(attemptId)')
  const disposeSource = extractFunction(source, 'function disposeIptvPlayback()')
  const factory = new Function('deps', `
    let _componentDisposed = false
    let _playAttemptId = 41
    let _recoverySeq = 8
    let _recoveryInFlight = true
    let _manualIptvStartPending = 2
    let _softPausedAt = 100
    let _softPauseReleased = true
    const {
      playerStore, iptvSourceRuntimeStatus, _racedLosers, _mpegtsRecoveries,
      clearPauseReleaseTimer, stopPlaybackWatchdogs, cancelCurrentStartup,
      cancelActiveProxyRace, destroyIptvEngines, resetIptvVideo,
      clearIptvMediaSession,
    } = deps
    ${isAttemptActiveSource}
    ${disposeSource}
    return {
      disposeIptvPlayback,
      isAttemptActive,
      snapshot: () => ({
        _componentDisposed,
        _playAttemptId,
        _recoverySeq,
        _recoveryInFlight,
        _manualIptvStartPending,
        _softPausedAt,
        _softPauseReleased,
      }),
    }
  `)

  const calls = []
  const resources = {
    hls: { destroyed: false, destroy() { this.destroyed = true; calls.push('hls.destroy') } },
    mpegts: { destroyed: false, destroy() { this.destroyed = true; calls.push('mpegts.destroy') } },
    youtube: {
      stopped: false,
      destroyed: false,
      stopVideo() { this.stopped = true; calls.push('youtube.stop') },
      destroy() { this.destroyed = true; calls.push('youtube.destroy') },
    },
    video: {
      paused: false,
      src: 'https://old.example/live.m3u8',
      pause() { this.paused = true; calls.push('video.pause') },
      removeAttribute(name) { if (name === 'src') this.src = ''; calls.push('video.remove-src') },
      load() { calls.push('video.load') },
    },
  }
  const timers = new Set(['stall', 'watchdog', 'recovery'])
  const listeners = new Set(['hls-error', 'mpegts-error', 'video-frame'])
  const playerStore = { iptvVideoEl: resources.video, isPlaying: false, isLoading: false, playbackError: '' }
  const iptvSourceRuntimeStatus = { value: { old: 'playing' } }
  const racedLosers = new Map([['old', Date.now() + 45_000]])
  const mpegtsRecoveries = new Map([['old', { count: 2 }]])

  const harness = factory({
    playerStore,
    iptvSourceRuntimeStatus,
    _racedLosers: racedLosers,
    _mpegtsRecoveries: mpegtsRecoveries,
    clearPauseReleaseTimer: () => { timers.delete('recovery'); calls.push('pause-timer.clear') },
    stopPlaybackWatchdogs: () => {
      timers.delete('stall')
      timers.delete('watchdog')
      listeners.delete('video-frame')
      calls.push('watchdogs.stop')
    },
    cancelCurrentStartup: () => { listeners.delete('hls-error'); calls.push('startup.cancel') },
    cancelActiveProxyRace: () => { listeners.delete('mpegts-error'); calls.push('race.cancel') },
    destroyIptvEngines: () => {
      resources.hls.destroy()
      resources.mpegts.destroy()
      resources.youtube.stopVideo()
      resources.youtube.destroy()
    },
    resetIptvVideo: () => {
      resources.video.pause()
      resources.video.removeAttribute('src')
      resources.video.load()
    },
    clearIptvMediaSession: () => calls.push('media-session.clear'),
  })

  return { calls, harness, iptvSourceRuntimeStatus, listeners, mpegtsRecoveries, playerStore, racedLosers, resources, timers }
}

test('App 以 authPage 为唯一入口显式停止共享播放状态', () => {
  const source = fs.readFileSync(appPath, 'utf8')
  assert.match(source, /const showAppShell = computed\(\(\) => !route\.meta\.authPage\)/)
  assert.match(source, /watch\(showAppShell,[\s\S]*?playerStore\.stopAndClearPlayback\(\)/)
})

test('stopAndClearPlayback 清空选择和运行态，并阻止旧 adapter resolve 回写', async () => {
  setActivePinia(createPinia())
  const response = deferred()
  globalThis.fetch = () => response.promise
  const store = usePlayerStore()
  store.currentStation = 'radio-old'
  store.currentIptvChannel = { name: 'old' }
  store.pendingIptvChannel = { name: 'pending' }
  store.iptvUrls = [{ url: 'https://old.example/live.m3u8' }]
  store.iptvUrlIndex = 3
  store.iptvVideoEl = { id: 'old-video' }
  store.currentEpgProgram = { title: 'old epg' }
  store.setIptvChannelContext({
    group: 'old-group',
    search: 'old-search',
    channels: [{ canonical_key: 'old', name: 'old' }],
  })
  store.isPlayerExpanded = true
  store.isPlaying = true
  store.isLoading = true
  store.playbackError = 'old error'

  const pendingPlay = store.playIptvChannel({
    canonical_key: 'adapter-test',
    urls: [{ url: 'huya://123', source_id: 'adapter-1', source_type: 'adapter' }],
  })
  const selectionToken = store.iptvSelectionToken

  store.stopAndClearPlayback()
  response.resolve({
    ok: true,
    json: async () => ({
      ok: true,
      url: 'https://late.example/live.flv',
      source_type: 'http_flv',
      direct_playable: true,
    }),
  })
  await pendingPlay

  assert.ok(store.iptvSelectionToken > selectionToken)
  assert.equal(store.currentStation, '')
  assert.equal(store.currentIptvChannel, null)
  assert.equal(store.pendingIptvChannel, null)
  assert.deepEqual(store.iptvUrls, [])
  assert.equal(store.iptvUrlIndex, 0)
  assert.equal(store.iptvVideoEl, null)
  assert.equal(store.currentEpgProgram, null)
  assert.equal(store.iptvChannelContext, null)
  assert.equal(store.isPlayerExpanded, false)
  assert.equal(store.isPlaying, false)
  assert.equal(store.isLoading, false)
  assert.equal(store.playbackError, '')
})

test('旧频道 selection 晚完成不能覆盖新频道和新列表上下文', async () => {
  setActivePinia(createPinia())
  const response = deferred()
  globalThis.fetch = () => response.promise
  const store = usePlayerStore()
  const channelA = {
    canonical_key: 'channel-a',
    name: 'A',
    urls: [{ url: 'huya://123', source_id: 'adapter-a', source_type: 'adapter' }],
  }
  const channelB = {
    canonical_key: 'channel-b',
    name: 'B',
    urls: [{ url: 'https://media.example/b.m3u8', source_id: 'hls-b', source_type: 'hls' }],
  }
  const pendingA = store.playIptvChannel(channelA, {
    channelContext: { group: 'A组', search: 'A', channels: [channelA] },
  })
  await store.playIptvChannel(channelB, {
    channelContext: { group: 'B组', search: 'B', channels: [channelB] },
  })

  response.resolve({
    ok: true,
    json: async () => ({
      ok: true,
      url: 'https://late.example/a.m3u8',
      source_type: 'hls',
      direct_playable: true,
    }),
  })
  await pendingA

  assert.equal(store.currentIptvChannel?.canonical_key, 'channel-b')
  assert.equal(store.pendingIptvChannel, null)
  assert.equal(store.iptvUrls[0]?.source_id, 'hls-b')
  assert.equal(store.iptvChannelContext?.group, 'B组')
  assert.equal(store.iptvChannelContext?.search, 'B')
  assert.deepEqual(store.iptvChannelContext?.channels.map((item) => item.canonical_key), ['channel-b'])
  assert.equal(store.playbackError, '')
})

test('进入 auth 后旧 adapter resolve 晚失败不会重新写 error', async () => {
  setActivePinia(createPinia())
  const response = deferred()
  globalThis.fetch = () => response.promise
  const store = usePlayerStore()
  const pendingPlay = store.playIptvChannel({
    canonical_key: 'adapter-error-test',
    urls: [{ url: 'huya://456', source_id: 'adapter-2', source_type: 'adapter' }],
  })

  store.stopAndClearPlayback()
  response.reject(new Error('late adapter failure'))
  await pendingPlay

  assert.equal(store.currentIptvChannel, null)
  assert.equal(store.pendingIptvChannel, null)
  assert.deepEqual(store.iptvUrls, [])
  assert.equal(store.isPlaying, false)
  assert.equal(store.isLoading, false)
  assert.equal(store.playbackError, '')
})

test('FullPlayer auth disposal 销毁所有引擎、网络生命周期、video 和运行态', async () => {
  const h = createFullPlayerDisposeHarness()
  const oldAttempt = h.harness.snapshot()._playAttemptId
  const lateCallback = deferred()
  const lateWrite = lateCallback.promise.then(() => {
    if (h.harness.isAttemptActive(oldAttempt)) {
      h.playerStore.isPlaying = true
      h.playerStore.isLoading = true
      h.playerStore.playbackError = 'late callback'
    }
  })

  h.harness.disposeIptvPlayback()
  lateCallback.resolve()
  await lateWrite

  const state = h.harness.snapshot()
  assert.equal(state._componentDisposed, true)
  assert.ok(state._playAttemptId > oldAttempt)
  assert.equal(state._recoverySeq, 9)
  assert.equal(state._recoveryInFlight, false)
  assert.equal(state._manualIptvStartPending, 0)
  assert.equal(state._softPausedAt, 0)
  assert.equal(state._softPauseReleased, false)
  assert.equal(h.harness.isAttemptActive(oldAttempt), false)
  assert.equal(h.resources.hls.destroyed, true)
  assert.equal(h.resources.mpegts.destroyed, true)
  assert.equal(h.resources.youtube.stopped, true)
  assert.equal(h.resources.youtube.destroyed, true)
  assert.equal(h.resources.video.paused, true)
  assert.equal(h.resources.video.src, '')
  assert.equal(h.playerStore.iptvVideoEl, null)
  assert.deepEqual(h.iptvSourceRuntimeStatus.value, {})
  assert.equal(h.racedLosers.size, 0)
  assert.equal(h.mpegtsRecoveries.size, 0)
  assert.equal(h.timers.size, 0)
  assert.equal(h.listeners.size, 0)
  assert.equal(h.playerStore.isPlaying, false)
  assert.equal(h.playerStore.isLoading, false)
  assert.equal(h.playerStore.playbackError, '')
  assert.ok(h.calls.includes('media-session.clear'))
})

test('连续五次 auth disposal 保持幂等，旧 attempt 永不恢复', () => {
  const h = createFullPlayerDisposeHarness()
  const oldAttempt = h.harness.snapshot()._playAttemptId
  for (let index = 0; index < 5; index += 1) h.harness.disposeIptvPlayback()

  assert.equal(h.harness.isAttemptActive(oldAttempt), false)
  assert.equal(h.playerStore.iptvVideoEl, null)
  assert.equal(h.timers.size, 0)
  assert.equal(h.listeners.size, 0)
})
