import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const mountedWrappers = []

let dom
let AudioEngine
let mount
let createPinia
let setActivePinia
let usePlayerStore
let flushPromises

function installDom() {
  dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', {
    url: 'http://localhost:5173/',
    pretendToBeVisual: true,
  })
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    Node: dom.window.Node,
    SVGElement: dom.window.SVGElement,
    HTMLElement: dom.window.HTMLElement,
    HTMLAudioElement: dom.window.HTMLAudioElement,
    HTMLMediaElement: dom.window.HTMLMediaElement,
    DocumentFragment: dom.window.DocumentFragment,
    DOMRect: dom.window.DOMRect,
    AbortController: dom.window.AbortController,
    AbortSignal: dom.window.AbortSignal,
    Event: dom.window.Event,
    CustomEvent: dom.window.CustomEvent,
    getComputedStyle: dom.window.getComputedStyle,
  })) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }

  globalThis.ResizeObserver = class { observe() {} disconnect() {} }
  globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} }
  globalThis.requestAnimationFrame = (callback) => setTimeout(() => callback(Date.now()), 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)

  let playCalls = 0
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'play', {
    configurable: true,
    value() {
      playCalls += 1
      Object.defineProperty(this, 'paused', { configurable: true, value: false })
      Object.defineProperty(this, 'readyState', { configurable: true, value: 4 })
      return Promise.resolve()
    },
  })
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'pause', {
    configurable: true,
    value() { Object.defineProperty(this, 'paused', { configurable: true, value: true }) },
  })
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'load', {
    configurable: true,
    value() {},
  })
  return { getPlayCalls: () => playCalls }
}

function fakeHlsModule() {
  return `
    class FakeHls {
      static Events = { ERROR: 'error', MANIFEST_PARSED: 'manifestParsed' }
      static isSupported() { return false }
      on() {}
      off() {}
      destroy() {}
    }
    export default FakeHls
  `
}

async function loadComponent() {
  const media = installDom()
  const [{ build }, { default: vue }, compiler] = await Promise.all([
    import('vite'),
    import('@vitejs/plugin-vue'),
    import('@vue/compiler-sfc'),
  ])
  const tmpDir = path.resolve('/tmp/waveflow-audio-engine-component-test')
  const tmpEntry = path.resolve('/tmp/waveflow-audio-engine-component-entry.mjs')
  fs.writeFileSync(tmpEntry, `
    import AudioEngine from ${JSON.stringify(path.join(frontendRoot, 'src/components/AudioEngine.vue'))}
    export default AudioEngine
  `)
  const fakePlugin = {
    name: 'audio-engine-test-media-mocks',
    enforce: 'pre',
    resolveId(id) {
      if (id === 'hls.js') return '\\0audio-engine-test-hls'
    },
    load(id) {
      if (id === '\\0audio-engine-test-hls') return fakeHlsModule()
    },
  }
  await build({
    root: frontendRoot,
    configFile: false,
    logLevel: 'error',
    plugins: [fakePlugin, vue({ compiler: compiler.default || compiler })],
    build: {
      outDir: tmpDir,
      emptyOutDir: true,
      lib: { entry: tmpEntry, formats: ['es'], fileName: 'audio-engine-component' },
      rollupOptions: {
        external: ['vue', 'pinia'],
        output: {
          paths: {
            vue: new URL('../../node_modules/vue/index.js', import.meta.url).href,
            pinia: new URL('../../node_modules/pinia/dist/pinia.mjs', import.meta.url).href,
          },
        },
      },
    },
  })

  const testUtils = await import('@vue/test-utils')
  const vueRuntime = await import('vue')
  const pinia = await import('pinia')
  const componentModule = await import(`${path.join(tmpDir, 'audio-engine-component.js')}?test=${Date.now()}`)
  mount = testUtils.mount
  flushPromises = testUtils.flushPromises
  createPinia = pinia.createPinia
  setActivePinia = pinia.setActivePinia
  usePlayerStore = (await import('../../src/stores/player.js')).usePlayerStore
  AudioEngine = componentModule.default
  return media
}

before(async () => {
  globalThis.__audioEngineComponentReady = await loadComponent()
})

afterEach(() => {
  while (mountedWrappers.length) mountedWrappers.pop().unmount()
  delete globalThis.fetch
})

after(() => {
  dom?.window.close()
})

test('一次动态 Radio 选台只创建一个 resolver/load/play attempt', async () => {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.addRadioStations([{
    id: 'radio_a',
    name: '测试电台',
    radioDomain: 'radio',
    radioStationId: 'radio_a',
    radioSourceId: 'source_a',
    radioSources: [{ source_id: 'source_a', lifecycle_state: 'active' }],
  }])

  let resolveCalls = 0
  let resolveSource
  const sourceReady = new Promise((resolve) => { resolveSource = resolve })
  globalThis.fetch = async (input) => {
    const url = String(input?.url || input)
    if (url.includes('/api/radio/stations/radio_a/resolve')) {
      resolveCalls += 1
      return { ok: true, json: async () => sourceReady }
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const wrapper = mount(AudioEngine, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  const playCallsBefore = globalThis.__audioEngineComponentReady.getPlayCalls()
  store.switchStation('radio_a')
  await flushPromises()

  assert.equal(resolveCalls, 1)
  assert.equal(globalThis.__audioEngineComponentReady.getPlayCalls() - playCallsBefore, 0)

  resolveSource({ source_type: 'audio_http' })
  await flushPromises()
  await flushPromises()

  assert.equal(globalThis.__audioEngineComponentReady.getPlayCalls() - playCallsBefore, 1)
  assert.equal(store.radioPlaybackIntent, 'passive')
  assert.equal(store.playbackError, '')
})

test('同一电台切换 Radio source 会重载一次，但新台的 source 初始化不重复重载', async () => {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.addRadioStations([{
    id: 'radio_b',
    name: '测试电台 B',
    radioDomain: 'radio',
    radioStationId: 'radio_b',
    radioSourceId: 'source_a',
    radioSources: [
      { source_id: 'source_a', lifecycle_state: 'active' },
      { source_id: 'source_b', lifecycle_state: 'active' },
    ],
  }])

  let resolveCalls = 0
  globalThis.fetch = async (input) => {
    const url = String(input?.url || input)
    if (url.includes('/api/radio/stations/radio_b/resolve')) {
      resolveCalls += 1
      return { ok: true, json: async () => ({ source_type: 'audio_http' }) }
    }
    throw new Error(`unexpected request: ${url}`)
  }

  const playCallsBefore = globalThis.__audioEngineComponentReady.getPlayCalls()
  const wrapper = mount(AudioEngine, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  store.switchStation('radio_b')
  await flushPromises()
  await flushPromises()
  assert.equal(resolveCalls, 1)

  store.selectRadioSource('radio_b', 'source_b')
  await flushPromises()
  await flushPromises()

  assert.equal(resolveCalls, 2)
  assert.equal(globalThis.__audioEngineComponentReady.getPlayCalls() - playCallsBefore, 2)
  assert.equal(store.stationMap.radio_b.radioSourceId, 'source_b')
})
