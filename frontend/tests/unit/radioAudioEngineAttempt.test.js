import test from 'node:test'
import assert from 'node:assert/strict'

import { createRadioAudioEngine } from '../../src/utils/radioAudioEngine.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
  await new Promise((resolve) => setImmediate(resolve))
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

class FakeAudio {
  constructor() {
    this._src = ''
    this.currentSrc = ''
    this.volume = 1
    this.listeners = new Map()
    this.playCalls = []
    this.pauseCalls = 0
    this.loadCalls = 0
    this.nextPlay = null
  }

  set src(value) {
    this._src = String(value || '')
    this.currentSrc = this._src
  }

  get src() { return this._src }

  addEventListener(name, callback, options = {}) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set())
    this.listeners.get(name).add({ callback, once: Boolean(options.once) })
  }

  removeEventListener(name, callback) {
    const listeners = this.listeners.get(name)
    if (!listeners) return
    for (const listener of listeners) if (listener.callback === callback) listeners.delete(listener)
  }

  emit(name) {
    for (const listener of Array.from(this.listeners.get(name) || [])) {
      if (listener.once) this.listeners.get(name).delete(listener)
      listener.callback()
    }
  }

  play() {
    this.playCalls.push(this.currentSrc)
    const next = this.nextPlay
    this.nextPlay = null
    return next ? next.promise : Promise.resolve()
  }

  pause() { this.pauseCalls += 1 }
  load() { this.loadCalls += 1 }
  removeAttribute(name) {
    if (name === 'src') {
      this.src = ''
    }
  }
  canPlayType() { return '' }
}

function hlsMock(supported) {
  const instances = []
  class Hls {
    static Events = { ERROR: 'ERROR', MANIFEST_PARSED: 'MANIFEST_PARSED' }
    static isSupported() { return supported }
    constructor(config) { this.config = config; this.handlers = new Map(); this.destroyed = false; instances.push(this) }
    on(name, callback) { if (!this.handlers.has(name)) this.handlers.set(name, new Set()); this.handlers.get(name).add(callback) }
    off(name, callback) { this.handlers.get(name)?.delete(callback) }
    emit(name, data) { for (const callback of this.handlers.get(name) || []) callback(name, data) }
    loadSource(url) { this.source = url }
    attachMedia(media) { this.media = media }
    destroy() { this.destroyed = true }
  }
  return { Hls, instances }
}

function response(value) { return { ok: true, json: async () => value } }

function radio(id, stationId = id, sourceId = `source-${id}`) {
  return { id, name: `Radio ${id}`, radioStationId: stationId, radioSourceId: sourceId }
}

function harness({ fetchImpl, hlsSupported = false } = {}) {
  const audio = new FakeAudio()
  const hls = hlsMock(hlsSupported)
  const currentStation = { value: '' }
  const store = {
    currentIptvChannel: null,
    stationMap: {},
    isPlaying: false,
    isLoading: false,
    playbackError: '',
    clearPlaybackError() { this.playbackError = '' },
    setLoading(value) { this.isLoading = Boolean(value) },
    setPlaybackError(value) { this.playbackError = value || ''; if (value) this.isLoading = false },
    togglePlay(value) { this.isPlaying = Boolean(value) },
  }
  const engine = createRadioAudioEngine({
    audioRef: { value: audio }, hlsRef: { value: null }, directStreamMode: { value: '' },
    playerStore: store, currentStation, volume: { value: 1 }, Hls: hls.Hls, API_BASE: '',
    fetchImpl, publicAsset: (value) => value, getNavigator: () => ({}), getMediaMetadata: () => undefined,
    createAudio: () => new FakeAudio(), logger: { warn() {}, log() {} },
  })
  return { audio, currentStation, store, engine, hls: hls.instances }
}

test('persisted audio HTTP source resolves only by source_id and plays the Core proxy URL', async () => {
  const calls = []
  const h = harness({ fetchImpl: async (url) => {
    calls.push(String(url))
    return response({ source_type: 'audio_http' })
  } })
  h.store.stationMap.one = radio('one', 'station-one', 'source-one')
  h.currentStation.value = 'one'

  h.engine.loadStation('one', { intent: 'station_click' })
  await flush()

  assert.deepEqual(calls, ['/api/radio/stations/station-one/resolve?source_id=source-one'])
  assert.deepEqual(h.audio.playCalls, ['/api/media/radio/station-one/stream?source_id=source-one'])
  assert.equal(h.store.playbackError, '')
})

test('persisted HLS source stays on the Core radio media route', async () => {
  const h = harness({ hlsSupported: true, fetchImpl: async () => response({ source_type: 'hls' }) })
  h.store.stationMap.one = radio('one', 'station-one', 'source-one')
  h.currentStation.value = 'one'

  h.engine.loadStation('one')
  await flush()

  assert.equal(h.hls.length, 1)
  assert.equal(h.hls[0].source, '/api/media/radio/station-one/playlist.m3u8?source_id=source-one')
  h.hls[0].emit('MANIFEST_PARSED')
  await flush()
  assert.deepEqual(h.audio.playCalls, [''])
})

test('late resolve from an old station cannot overwrite a newer station attempt', async () => {
  const first = deferred()
  const h = harness({ fetchImpl: async (url) => {
    if (String(url).includes('station-one')) return first.promise
    return response({ source_type: 'audio_http' })
  } })
  h.store.stationMap.one = radio('one', 'station-one', 'source-one')
  h.store.stationMap.two = radio('two', 'station-two', 'source-two')
  h.currentStation.value = 'one'
  h.engine.loadStation('one')
  await flush()

  h.currentStation.value = 'two'
  h.engine.loadStation('two')
  await flush()
  first.resolve(response({ source_type: 'audio_http' }))
  await flush()

  assert.deepEqual(h.audio.playCalls, ['/api/media/radio/station-two/stream?source_id=source-two'])
  assert.equal(h.engine.activeAttemptInfo().stationId, 'two')
})

test('failed resolve releases its attempt so explicit play can retry', async () => {
  let calls = 0
  const h = harness({ fetchImpl: async () => { calls += 1; return { ok: false, json: async () => ({}) } } })
  h.store.stationMap.one = radio('one')
  h.currentStation.value = 'one'

  h.engine.loadStation('one', { intent: 'station_click' })
  await flush()
  assert.equal(h.engine.activeAttemptInfo(), null)
  assert.equal(h.store.playbackError, '电台播放源解析失败，请稍后重试。')

  h.store.playbackError = ''
  h.engine.loadStation('one', { intent: 'play_button' })
  await flush()
  assert.equal(calls, 2)
})

test('a removed or non-persisted station never falls back to legacy endpoints', () => {
  const h = harness({ fetchImpl: async () => { throw new Error('must not fetch') } })
  h.currentStation.value = 'gone'
  h.engine.loadStation('gone')

  assert.equal(h.store.playbackError, '电台目录已更新，请从当前目录重新选择电台。')
  assert.equal(h.engine.activeAttemptInfo(), null)
})

test('stop invalidates a pending resolve and prevents late media playback', async () => {
  const pending = deferred()
  const h = harness({ fetchImpl: async () => pending.promise })
  h.store.stationMap.one = radio('one')
  h.currentStation.value = 'one'
  h.engine.loadStation('one')
  await flush()

  h.currentStation.value = ''
  h.engine.stopRadioAttempt()
  pending.resolve(response({ source_type: 'audio_http' }))
  await flush()

  assert.deepEqual(h.audio.playCalls, [])
  assert.equal(h.engine.activeAttemptInfo(), null)
})
