import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const fullPlayerPath = new URL('../../src/components/FullPlayer.vue', import.meta.url)

function extractBetween(source, startSignature, endSignature) {
  const start = source.indexOf(startSignature)
  assert.notEqual(start, -1, `missing start: ${startSignature}`)
  const end = source.indexOf(endSignature, start)
  assert.notEqual(end, -1, `missing end: ${endSignature}`)
  return source.slice(start, end).trim()
}

class FakeEmitter {
  constructor() {
    this.handlers = new Map()
    this.destroyed = false
  }

  on(event, handler) {
    const handlers = this.handlers.get(event) || new Set()
    handlers.add(handler)
    this.handlers.set(event, handlers)
  }

  off(event, handler) {
    this.handlers.get(event)?.delete(handler)
  }

  emit(event, ...args) {
    for (const handler of [...(this.handlers.get(event) || [])]) handler(...args)
  }

  listenerCount() {
    return [...this.handlers.values()].reduce((sum, handlers) => sum + handlers.size, 0)
  }

  destroy() {
    this.destroyed = true
  }
}

function createRuntimeHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const hlsSource = extractBetween(
    source,
    'function attachRuntimeHlsErrorHandlers(',
    '\nfunction attachRuntimeMpegtsErrorHandlers(',
  )
  const mpegtsSource = extractBetween(
    source,
    'function attachRuntimeMpegtsErrorHandlers(',
    '\nasync function tryPlayIptv(',
  )
  const factory = new Function('deps', `
    let _playAttemptId = 1
    let _componentDisposed = false
    const { calls, timers, iptvVideoRef } = deps
    const Hls = {
      Events: { FRAG_LOADED: 'frag-loaded', ERROR: 'error' },
      ErrorDetails: {
        FRAG_LOAD_ERROR: 'frag-load-error', FRAG_LOAD_TIMEOUT: 'frag-load-timeout',
        KEY_LOAD_ERROR: 'key-load-error', KEY_LOAD_TIMEOUT: 'key-load-timeout',
        LEVEL_LOAD_ERROR: 'level-load-error', LEVEL_LOAD_TIMEOUT: 'level-load-timeout',
        MANIFEST_LOAD_ERROR: 'manifest-load-error', MANIFEST_LOAD_TIMEOUT: 'manifest-load-timeout',
        LEVEL_PARSING_ERROR: 'level-parsing-error', BUFFER_STALLED_ERROR: 'buffer-stalled-error',
        BUFFER_NUDGE_ON_STALL: 'buffer-nudge-on-stall',
      },
      ErrorTypes: { NETWORK_ERROR: 'network-error', MEDIA_ERROR: 'media-error' },
    }
    const mpegts = { Events: { ERROR: 'error', LOADING_COMPLETE: 'loading-complete' } }
    const MPEGTS_ERROR_RECONNECT_DELAY_MS = 1000
    const MPEGTS_EOF_RECONNECT_DELAY_MS = 1200
    const isAttemptActive = (attemptId) => !_componentDisposed && attemptId === _playAttemptId
    const setSourceRuntimeStatus = (index, status) => calls.push('status:' + index + ':' + status)
    const setSourceRuntimeStatusByUrl = (_url, status) => calls.push('status:url:' + status)
    const clearCurrentHlsIf = () => calls.push('hls.clear')
    const clearCurrentMpegtsIf = () => calls.push('mpegts.clear')
    const releaseTrackedHls = () => calls.push('hls.release')
    const recoverIptvPlayback = async (reason) => calls.push('recover:' + reason)
    const playerStore = {
      setPlaybackError: (value) => calls.push('error:' + value),
      setLoading: (value) => calls.push('loading:' + value),
    }
    const setTimeout = (callback, delay) => {
      const timer = { callback, delay, cleared: false }
      timers.add(timer)
      return timer
    }
    const clearTimeout = (timer) => {
      if (!timer) return
      timer.cleared = true
      timers.delete(timer)
    }
    const clearRuntimeHandlerCleanup = (target) => {
      const cleanup = target?.__waveflowRuntimeCleanup
      if (cleanup) cleanup()
    }
    ${hlsSource}
    ${mpegtsSource}
    return {
      attachRuntimeHlsErrorHandlers,
      attachRuntimeMpegtsErrorHandlers,
      clearRuntimeHandlerCleanup,
      setAttempt: (value) => { _playAttemptId = value },
    }
  `)

  const calls = []
  const timers = new Set()
  const harness = factory({
    calls,
    timers,
    iptvVideoRef: { value: { currentTime: 10, readyState: 2, networkState: 2 } },
  })
  return { calls, harness, timers }
}

test('旧 HLS ERROR 晚到不写状态、不触发 recovery，cleanup 移除 listener', () => {
  const h = createRuntimeHarness()
  const player = new FakeEmitter()
  h.harness.attachRuntimeHlsErrorHandlers(player, 'https://old/live.m3u8', false, 1, 0)
  assert.equal(player.listenerCount(), 2)

  h.harness.setAttempt(2)
  for (let index = 0; index < 5; index += 1) {
    player.emit('error', null, {
      fatal: true,
      type: 'network-error',
      details: 'manifest-load-error',
    })
  }

  assert.deepEqual(h.calls, [])
  assert.equal(player.destroyed, false)
  h.harness.clearRuntimeHandlerCleanup(player)
  assert.equal(player.listenerCount(), 0)
})

test('旧 mpegts ERROR/LOADING_COMPLETE 晚到不写状态且不创建 reconnect timer', () => {
  const h = createRuntimeHarness()
  const player = new FakeEmitter()
  h.harness.attachRuntimeMpegtsErrorHandlers(player, 'https://old/live.flv', false, 1, 0)
  assert.equal(player.listenerCount(), 2)

  h.harness.setAttempt(2)
  player.emit('error', 'NetworkError', 'late')
  player.emit('loading-complete')

  assert.deepEqual(h.calls, [])
  assert.equal(h.timers.size, 0)
  assert.equal(player.destroyed, false)
  h.harness.clearRuntimeHandlerCleanup(player)
  assert.equal(player.listenerCount(), 0)
})

test('mpegts reconnect timer 晚到时再次检查 attempt，不接管新 source', async () => {
  const h = createRuntimeHarness()
  const player = new FakeEmitter()
  h.harness.attachRuntimeMpegtsErrorHandlers(player, 'https://old/live.flv', false, 1, 0)

  player.emit('error', 'NetworkError', 'disconnect')
  assert.equal(h.timers.size, 1)
  h.harness.setAttempt(2)
  const [timer] = h.timers
  h.timers.delete(timer)
  await timer.callback()

  assert.equal(h.calls.filter((call) => call.startsWith('recover:')).length, 0)
})
