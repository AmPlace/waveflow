import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { sourceRaceKey } from '../../src/utils/sourceIdentity.js'

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

function createStallTimerHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const scheduleSource = extractBetween(
    source,
    'function scheduleStallRecovery(',
    '\nfunction onVideoTimeUpdate(',
  )
  const factory = new Function('deps', `
    let _stallRecoveryTimer = null
    let _stallRecovering = false
    let _lastVideoProgressAt = 1000
    let now = 1000
    const { calls, timers, iptvVideoRef, isIptvMode, isPlaying } = deps
    const RECOVERY_LOADING_DELAY_MS = 100
    const RECOVERY_SOFT_RECOVER_MS = 200
    const RECOVERY_HARD_RELOAD_MS = 300
    const Date = { now: () => now }
    const playerStore = { setLoading: (value) => calls.push('loading:' + value) }
    const markVideoProgress = () => { _lastVideoProgressAt = now }
    const doRecovery = async (_video, reason) => calls.push('soft:' + reason)
    const recoverIptvPlayback = async (reason) => calls.push('hard:' + reason)
    const setTimeout = (callback, delay) => {
      const timer = { callback, delay }
      timers.add(timer)
      return timer
    }
    const clearTimeout = (timer) => timers.delete(timer)
    ${scheduleSource}
    return {
      scheduleStallRecovery,
      advanceTo: (value) => { now = value },
    }
  `)

  const calls = []
  const timers = new Set()
  const harness = factory({
    calls,
    timers,
    iptvVideoRef: { value: { paused: false } },
    isIptvMode: { value: true },
    isPlaying: { value: true },
  })
  return { calls, harness, timers }
}

function createFrameHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const stopSource = extractFunction(source, 'function stopVideoFrameWatch()')
  const startSource = extractFunction(source, 'function startVideoFrameWatch(')
  const factory = new Function('deps', `
    let _videoFrameCallbackId = null
    let _videoFrameWatchTimer = null
    let _videoFrameWatchVideo = null
    let _videoFrameWatchSeq = 0
    let _lastVideoFrameAt = 0
    let _lastPresentedFrames = 0
    let _lastVideoFrameMediaTime = 0
    let _lastVideoProgressAt = 0
    let _lastBufferNudgeTime = 0
    const { isIptvMode, isPlaying, intervals } = deps
    const isIOS = true
    const Date = { now: () => 1000 }
    const markVideoProgress = () => { _lastVideoProgressAt = 1000 }
    const getForwardBuffer = () => 1
    const getLiveLatency = () => 0
    const seekToStableLivePoint = () => false
    const recoverAvSync = () => {}
    const scheduleStallRecovery = () => {}
    const reconnectCurrentIptvSource = () => {}
    const setInterval = (callback, delay) => {
      const timer = { callback, delay }
      intervals.add(timer)
      return timer
    }
    const clearInterval = (timer) => intervals.delete(timer)
    ${stopSource}
    ${startSource}
    return { startVideoFrameWatch, stopVideoFrameWatch }
  `)

  const callbacks = new Map()
  const cancelled = []
  const intervals = new Set()
  let nextId = 1
  const video = {
    paused: false,
    ended: false,
    currentTime: 0,
    requestVideoFrameCallback(callback) {
      const id = nextId++
      callbacks.set(id, callback)
      return id
    },
    cancelVideoFrameCallback(id) {
      cancelled.push(id)
      callbacks.delete(id)
    },
  }
  const harness = factory({
    isIptvMode: { value: true },
    isPlaying: { value: true },
    intervals,
  })
  return { callbacks, cancelled, harness, intervals, video }
}

function createLoserHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const loserSource = extractBetween(
    source,
    'function pruneRaceLosers(',
    '\nfunction isProxyLikeEntry(',
  )
  const factory = new Function('deps', `
    const { sourceRaceKey } = deps
    const _racedLosers = new Map()
    const RACE_LOSER_TTL_MS = 45000
    let now = 1000
    const Date = { now: () => now }
    ${loserSource}
    return {
      pruneRaceLosers, isRaceLoser, markRaceLoser, clearRaceLoser, resetRacedLosers,
      advanceTo: (value) => { now = value },
      size: () => _racedLosers.size,
    }
  `)
  return factory({ sourceRaceKey })
}

test('重复 waiting/stalled 调度只保留一个 stall timer，并只触发一次 recovery', async () => {
  const h = createStallTimerHarness()
  h.harness.scheduleStallRecovery('waiting')
  h.harness.scheduleStallRecovery('stalled')
  assert.equal(h.timers.size, 1)

  h.harness.advanceTo(1401)
  const [timer] = h.timers
  h.timers.delete(timer)
  await timer.callback()

  assert.equal(h.calls.filter((call) => call.startsWith('soft:')).length, 1)
  assert.equal(h.calls.filter((call) => call.startsWith('hard:')).length, 1)
  assert.equal(h.timers.size, 0)
})

test('切台停止 frame watch 后旧 requestVideoFrameCallback 不会重新注册', () => {
  const h = createFrameHarness()
  h.harness.startVideoFrameWatch(h.video)
  const [[oldId, oldCallback]] = h.callbacks.entries()
  assert.equal(h.intervals.size, 1)

  h.harness.stopVideoFrameWatch()
  assert.deepEqual(h.cancelled, [oldId])
  assert.equal(h.intervals.size, 0)
  oldCallback(0, { presentedFrames: 1, mediaTime: 1 })
  assert.equal(h.callbacks.size, 0)
})

test('loser TTL 按 source + transport 隔离，手动选择和切回频道可立即清除', () => {
  const h = createLoserHarness()
  const direct = { source_id: 'same-source', type: 'direct', url: 'https://token-1/live.m3u8' }
  const proxy = { source_id: 'same-source', type: 'proxy', via_proxy: true, url: '/api/media/channel/a/playlist.m3u8' }

  h.markRaceLoser(direct)
  h.markRaceLoser(proxy)
  assert.equal(h.isRaceLoser(direct), true)
  assert.equal(h.isRaceLoser(proxy), true)
  assert.equal(h.size(), 2)

  h.clearRaceLoser(direct)
  assert.equal(h.isRaceLoser(direct), false)
  assert.equal(h.isRaceLoser(proxy), true)

  h.resetRacedLosers()
  assert.equal(h.size(), 0)
})

test('loser TTL 无独立过期 timer，过期后惰性清理且动态 token 不改变逻辑身份', () => {
  const h = createLoserHarness()
  const entry = { source_id: 'volatile', type: 'direct', url: 'https://token-1/live.m3u8' }
  h.markRaceLoser(entry)
  entry.url = 'https://token-2/live.m3u8'
  assert.equal(h.isRaceLoser(entry), true)

  h.advanceTo(46_001)
  assert.equal(h.isRaceLoser(entry), false)
  assert.equal(h.size(), 0)
})
