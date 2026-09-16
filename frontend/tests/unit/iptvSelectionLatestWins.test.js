import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { createPinia, setActivePinia } from 'pinia'

import { usePlayerStore } from '../../src/stores/player.js'

const fullPlayerPath = new URL('../../src/components/FullPlayer.vue', import.meta.url)

function deferred() {
  let resolve
  const promise = new Promise((res) => { resolve = res })
  return { promise, resolve }
}

function extractWatcherCallback(source) {
  const signature = 'watch(() => playerStore.currentIptvChannel, async (ch) => {'
  const start = source.indexOf(signature)
  assert.notEqual(start, -1, 'missing IPTV channel watcher')
  const bodyStart = source.indexOf('{', start)
  let depth = 0
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(bodyStart, index + 1)
  }
  throw new Error('unterminated IPTV channel watcher')
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

function createWatcherHarness(playerStore) {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const callbackBody = extractWatcherCallback(source)
  const firstTick = deferred()
  let tickCount = 0
  const starts = []
  const factory = new Function('deps', `
    let _playAttemptId = 0
    let _manualIptvStartPending = 0
    const {
      playerStore, nextTick, starts,
    } = deps
    let _playSelectionToken = playerStore.iptvSelectionToken
    const iptvVideoRef = { value: {} }
    const sourceMenuOpen = { value: true }
    const iptvSourceRuntimeStatus = { value: { stale: 'playing' } }
    const resetIptvVideo = () => {}
    const destroyIptvEngines = () => {}
    const resetRacedLosers = () => {}
    const playCurrentIptvUrl = async (attemptId) => {
      starts.push({ attemptId, channel: playerStore.currentIptvChannel?.canonical_key || '' })
    }
    const onCurrentIptvChannelChange = async (ch) => ${callbackBody}
    return { onCurrentIptvChannelChange }
  `)
  const harness = factory({
    playerStore,
    starts,
    nextTick: () => {
      tickCount += 1
      return tickCount === 1 ? firstTick.promise : Promise.resolve()
    },
  })
  return { firstTick, harness, starts }
}

function hlsChannel(key) {
  return {
    canonical_key: key,
    urls: [{
      url: `https://cdn.example/${key}.m3u8`,
      source_id: `src_${key}`,
      source_type: 'hls',
    }],
  }
}

test('快速选择 A 后 B 时，A 的 watcher 晚到不得创建第二条正式播放管线', async () => {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  const h = createWatcherHarness(store)

  const channelA = hlsChannel('A')
  const channelB = hlsChannel('B')
  await store.playIptvChannel(channelA)
  const watchedA = store.currentIptvChannel
  const lateA = h.harness.onCurrentIptvChannelChange(watchedA)

  await store.playIptvChannel(channelB)
  const watchedB = store.currentIptvChannel
  await h.harness.onCurrentIptvChannelChange(watchedB)
  h.firstTick.resolve()
  await lateA

  assert.deepEqual(h.starts, [{ attemptId: 1, channel: 'B' }])
  assert.equal(store.currentIptvChannel.canonical_key, channelB.canonical_key)
  assert.equal(store.pendingIptvChannel, null)
  assert.equal(store.playbackError, '')
})

test('播放页频道列表切台必须更新 selection owner 后再正式起播', async () => {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const functionSource = extractFunction(source, 'async function playIptvChannelFromFullPlayer(channel)')
  const starts = []
  const video = { play: () => Promise.resolve() }
  const channel = hlsChannel('full-player')
  const playerStore = {
    iptvVideoEl: video,
    currentIptvChannel: null,
    pendingIptvChannel: null,
    iptvSelectionToken: 0,
    setPlaybackError() {},
    async playIptvChannel(nextChannel) {
      this.iptvSelectionToken += 1
      this.currentIptvChannel = nextChannel
    },
  }
  const factory = new Function('deps', `
    const { playerStore, video, starts } = deps
    let _playAttemptId = 0
    let _playSelectionToken = playerStore.iptvSelectionToken
    let _manualIptvStartPending = 0
    const iptvVideoRef = { value: video }
    const toastStore = { info() {} }
    const isIptvUnavailable = () => false
    const isIptvAllNotLive = () => false
    const resetRacedLosers = () => {}
    const isAttemptActive = (attemptId) => (
      attemptId === _playAttemptId
      && _playSelectionToken === playerStore.iptvSelectionToken
    )
    const playCurrentIptvUrl = async (attemptId) => {
      if (isAttemptActive(attemptId)) starts.push(attemptId)
    }
    const nextTick = (callback) => Promise.resolve().then(callback)
    ${functionSource}
    return { playIptvChannelFromFullPlayer }
  `)
  const harness = factory({ playerStore, video, starts })

  await harness.playIptvChannelFromFullPlayer(channel)
  await Promise.resolve()

  assert.deepEqual(starts, [1])
})

test('频道进入 pending 时必须立即 hard teardown，而不是只 pause 旧视频', () => {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const teardown = extractFunction(source, 'function beginIptvChannelSwitch()')
  assert.match(teardown, /destroyIptvEngines\(\)/)
  assert.match(teardown, /resetIptvVideo\(\)/)
  assert.match(teardown, /cancelCurrentStartup\(\)/)
  assert.match(teardown, /cancelActiveProxyRace\(\)/)

  const fullPlayerSwitch = extractFunction(source, 'async function playIptvChannelFromFullPlayer(channel)')
  assert.doesNotMatch(fullPlayerSwitch, /videoEl\.play\(\)/)
  assert.match(source, /watch\(\(\) => playerStore\.pendingIptvChannel, \(pending\) => \{/)

  const homeSource = fs.readFileSync(new URL('../../src/views/IptvHome.vue', import.meta.url), 'utf8')
  const homeStart = homeSource.indexOf('async function playChannel(ch)')
  assert.notEqual(homeStart, -1)
  const homeEnd = homeSource.indexOf('\n}', homeStart)
  assert.doesNotMatch(homeSource.slice(homeStart, homeEnd), /videoEl\.play\(\)/)
})
