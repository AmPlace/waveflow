// Real unit tests for the IPTV fallback cycle.
//
// These deliberately call the store's own ``iptvFallbackNext`` instead of
// stubbing it out.  Every earlier suite that touched ``fallbackToNextIptvUrl``
// mocked the function, which is why the old "only ever walks forward" behaviour
// -- and the false "所有播放源均不可用" it produced for a late manual pick --
// was never caught.
import test from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'

import { usePlayerStore } from '../../src/stores/player.js'
import { PLAYBACK_ERROR } from '../../src/utils/playbackError.js'

function queueOf(count) {
  return Array.from({ length: count }, (_, i) => ({
    url: `https://media.example/source-${i}.m3u8`,
    source_id: `src_${i}`,
    enabled: true,
  }))
}

function storeWithSources(count, startIndex = 0) {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.iptvUrls = queueOf(count)
  store.iptvUrlIndex = startIndex
  return store
}

// Drain the cycle, recording the index each fallback landed on.
function drain(store, limit = 20) {
  const visited = [store.iptvUrlIndex]
  let guard = 0
  while (guard++ < limit) {
    if (!store.iptvFallbackNext()) break
    visited.push(store.iptvUrlIndex)
  }
  return visited
}

test('从第一个源开始：按 0..n-1 顺序走完整个环', () => {
  const store = storeWithSources(6, 0)
  assert.deepEqual(drain(store), [0, 1, 2, 3, 4, 5])
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)
})

test('从中间源开始：先向后，再回绕到前面的源', () => {
  const store = storeWithSources(6, 2)
  assert.deepEqual(drain(store), [2, 3, 4, 5, 0, 1])
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)
})

test('从最后一个源开始：仍然回绕尝试前面的源（P0 回归）', () => {
  const store = storeWithSources(6, 5)
  // 旧实现只递增到 length-1，于是直接报“所有播放源均不可用”，
  // 前面的 1..5 从未被尝试。
  assert.deepEqual(drain(store), [5, 0, 1, 2, 3, 4])
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)
})

test('单源：唯一一个源失败后立即判定不可用', () => {
  const store = storeWithSources(1, 0)
  assert.deepEqual(drain(store), [0])
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)
})

test('全失败：每个源在一轮 cycle 中最多尝试一次', () => {
  const store = storeWithSources(6, 3)
  const visited = drain(store)
  assert.equal(new Set(visited).size, visited.length, '出现了重复尝试')
  assert.equal(visited.length, 6)
  assert.equal(store.iptvFallbackVisited.length, 6)
})

test('中途成功：停在成功的源上，不继续切换', () => {
  const store = storeWithSources(6, 5)
  // 模拟：切到第 2 个 fallback（index 1）时播放成功，调用方不再继续。
  assert.equal(store.iptvFallbackNext(), true) // -> 0
  assert.equal(store.iptvUrlIndex, 0)
  assert.equal(store.iptvFallbackNext(), true) // -> 1
  assert.equal(store.iptvUrlIndex, 1)
  // 成功：调用方停止 fallback 并清掉错误
  store.clearPlaybackError()
  assert.equal(store.iptvUrlIndex, 1)
  assert.equal(store.playbackError, '')
  assert.equal(store.playbackErrorCode, '')
})

test('空队列：直接判定没有可播放的源', () => {
  const store = storeWithSources(0, 0)
  assert.equal(store.iptvFallbackNext(), false)
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.NO_PLAYABLE_SOURCE)
})

test('新 selection 重置 cycle：已经失败过的源可以重新尝试', () => {
  const store = storeWithSources(6, 5)
  drain(store)
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)
  assert.equal(store.iptvFallbackVisited.length, 6)

  // 用户重新手选 / 切频道 / 新 selection token
  const currentIndex = store.iptvUrlIndex
  store.resetIptvFallbackCycle()
  assert.deepEqual(store.iptvFallbackVisited, [])
  // 重置 cycle 不改变当前源：用户仍然停在刚选的那个源上。
  assert.equal(store.iptvUrlIndex, currentIndex)

  const again = drain(store)
  assert.equal(again[0], currentIndex, '重置后应从当前源重新开始')
  assert.equal(new Set(again).size, 6, '重置后应能重新走完整环')
  assert.equal(store.iptvFallbackVisited.length, 6)
})

test('切换频道会重置 cycle', () => {
  const store = storeWithSources(6, 5)
  drain(store)
  assert.equal(store.iptvFallbackVisited.length, 6)
  store.switchStation('cctv1')
  assert.deepEqual(store.iptvFallbackVisited, [])
})

test('停止播放会重置 cycle', () => {
  const store = storeWithSources(6, 5)
  drain(store)
  store.stopAndClearPlayback()
  assert.deepEqual(store.iptvFallbackVisited, [])
})

test('队列追加新源后，新源仍可被尝试（已试过的源不重复）', () => {
  const store = storeWithSources(3, 0)
  drain(store)
  assert.equal(store.playbackErrorCode, PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE)

  // progressive adapter 追加了一个新源
  store.iptvUrls = [...store.iptvUrls, {
    url: 'https://media.example/source-3.m3u8', source_id: 'src_3', enabled: true,
  }]
  assert.equal(store.iptvFallbackNext(), true)
  assert.equal(store.iptvUrlIndex, 3)
  assert.equal(store.iptvFallbackVisited.length, 4)
})
