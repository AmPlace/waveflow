import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { JSDOM } from 'jsdom'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

let dom
let builtComponentPath
let FullPlayer
let IptvHome
let mount
let createPinia
let setActivePinia
let usePlayerStore
let flushPromises
let vueRef
const mountedWrappers = []

const channel = (name, sourceId, extra = {}) => ({
  name,
  canonical_key: name.toLowerCase(),
  group_name: '测试',
  urls: [{
    url: `https://media.example/${sourceId}.m3u8`,
    source_id: sourceId,
    source_type: 'hls',
    probe_status: 'online',
    is_working: 1,
  }],
  ...extra,
})

const response = (body, init = {}) => ({
  ok: true,
  status: 200,
  json: async () => body,
  ...init,
})

function installDom() {
  dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', {
    url: 'http://localhost:5173/',
    pretendToBeVisual: true,
  })
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    Element: dom.window.Element,
    Node: dom.window.Node,
    SVGElement: dom.window.SVGElement,
    HTMLElement: dom.window.HTMLElement,
    HTMLIFrameElement: dom.window.HTMLIFrameElement,
    HTMLVideoElement: dom.window.HTMLVideoElement,
    HTMLMediaElement: dom.window.HTMLMediaElement,
    DocumentFragment: dom.window.DocumentFragment,
    DOMRect: dom.window.DOMRect,
    XMLHttpRequest: dom.window.XMLHttpRequest,
    MutationObserver: dom.window.MutationObserver,
    AbortController: dom.window.AbortController,
    AbortSignal: dom.window.AbortSignal,
    Event: dom.window.Event,
    CustomEvent: dom.window.CustomEvent,
    getComputedStyle: dom.window.getComputedStyle,
  }
  for (const [key, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  Object.defineProperty(dom.window.HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() { return 1024 },
  })
  dom.window.HTMLElement.prototype.getBoundingClientRect = function () {
    return {
      width: 100,
      height: 44,
      top: 0,
      left: 0,
      right: 100,
      bottom: 44,
      x: 0,
      y: 0,
      toJSON() {},
    }
  }
  globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
  }
  globalThis.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.requestAnimationFrame = (callback) => setTimeout(() => callback(Date.now()), 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)
  window.requestAnimationFrame = globalThis.requestAnimationFrame
  window.cancelAnimationFrame = globalThis.cancelAnimationFrame
  window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} })
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'play', {
    configurable: true,
    value() {
      if (!Object.prototype.hasOwnProperty.call(this, '__testCurrentTimeInstalled')) {
        const initialTime = Number(this.currentTime || 0)
        this.__testCurrentTimeInstalled = true
        this.__testCurrentTimeBase = initialTime
        this.__testCurrentTimeStartedAt = Date.now()
        Object.defineProperty(this, 'currentTime', {
          configurable: true,
          get() {
            if (this.paused) return this.__testCurrentTimeBase
            return this.__testCurrentTimeBase + ((Date.now() - this.__testCurrentTimeStartedAt) / 1000)
          },
          set(value) {
            this.__testCurrentTimeBase = Number(value) || 0
            this.__testCurrentTimeStartedAt = Date.now()
          },
        })
      }
      Object.defineProperty(this, 'paused', { configurable: true, value: false })
      Object.defineProperty(this, 'readyState', { configurable: true, value: 4 })
      this.__testCurrentTimeStartedAt = Date.now()
      Object.defineProperty(this, 'buffered', {
        configurable: true,
        value: { length: 1, start: () => 0, end: () => this.currentTime + 1 },
      })
      return Promise.resolve()
    },
  })
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'pause', {
    configurable: true,
    value() {
      const currentTime = Number(this.currentTime || 0)
      this.__testCurrentTimeBase = currentTime
      Object.defineProperty(this, 'paused', { configurable: true, value: true })
    },
  })
  Object.defineProperty(dom.window.HTMLMediaElement.prototype, 'load', {
    configurable: true,
    value() {},
  })
}

function fakeHlsModule() {
  return `
    class FakeHls {
      static Events = {
        MEDIA_ATTACHED: 'mediaAttached',
        MANIFEST_PARSED: 'manifestParsed',
        FRAG_BUFFERED: 'fragBuffered',
        FRAG_LOADED: 'fragLoaded',
        ERROR: 'error',
      }
      static ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' }
      static ErrorDetails = {
        FRAG_LOAD_ERROR: 'fragLoadError', FRAG_LOAD_TIMEOUT: 'fragLoadTimeout',
        KEY_LOAD_ERROR: 'keyLoadError', KEY_LOAD_TIMEOUT: 'keyLoadTimeout',
        LEVEL_LOAD_ERROR: 'levelLoadError', LEVEL_LOAD_TIMEOUT: 'levelLoadTimeout',
        MANIFEST_LOAD_ERROR: 'manifestLoadError', MANIFEST_LOAD_TIMEOUT: 'manifestLoadTimeout',
        LEVEL_PARSING_ERROR: 'levelParsingError', BUFFER_STALLED_ERROR: 'bufferStalledError',
        BUFFER_NUDGE_ON_STALL: 'bufferNudgeOnStall',
      }
      static isSupported() { return true }
      constructor() { this.listeners = new Map(); this.destroyed = false }
      on(name, fn) { const list = this.listeners.get(name) || []; list.push(fn); this.listeners.set(name, list) }
      off(name, fn) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== fn)) }
      emit(name, ...args) { for (const fn of [...(this.listeners.get(name) || [])]) fn(...args) }
      attachMedia(video) {
        this.video = video
        globalThis.__fullPlayerHlsCalls = globalThis.__fullPlayerHlsCalls || []
        globalThis.__fullPlayerHlsCalls.push({ url: this.url, formal: video.classList.contains('media-video') })
        queueMicrotask(() => {
          if (this.destroyed) return
          this.emit(FakeHls.Events.MEDIA_ATTACHED)
          this.emit(FakeHls.Events.MANIFEST_PARSED)
          this.emit(FakeHls.Events.FRAG_LOADED)
          this.emit(FakeHls.Events.FRAG_BUFFERED)
        })
      }
      loadSource(url) { this.url = url }
      startLoad() {}
      recoverMediaError() {}
      destroy() { this.destroyed = true; this.listeners.clear() }
    }
    export default FakeHls
  `
}

function fakeMpegtsModule() {
  return `
    const Events = { MEDIA_INFO: 'mediaInfo', STATISTICS_INFO: 'statisticsInfo', ERROR: 'error', LOADING_COMPLETE: 'loadingComplete' }
    export default {
      Events,
      isSupported: () => true,
      getFeatureList: () => ({ mseLivePlayback: true }),
      createPlayer: () => ({
        listeners: new Map(),
        on(name, fn) { const list = this.listeners.get(name) || []; list.push(fn); this.listeners.set(name, list) },
        off(name, fn) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== fn)) },
        attachMediaElement(video) { this.video = video },
        load() { queueMicrotask(() => this.listeners.get(Events.MEDIA_INFO)?.forEach(fn => fn())) },
        play() { return Promise.resolve() },
        destroy() { this.destroyed = true; this.listeners.clear() },
      }),
    }
  `
}

async function loadComponent() {
  installDom()
  const [{ build }, { default: vue }, compiler] = await Promise.all([
    import('vite'),
    import('@vitejs/plugin-vue'),
    import('@vue/compiler-sfc'),
  ])
  const tmpDir = path.resolve('/tmp/waveflow-full-player-dom-test')
  const tmpEntry = path.resolve('/tmp/waveflow-full-player-dom-entry.mjs')
  fs.writeFileSync(tmpEntry, `
    import FullPlayer from ${JSON.stringify(path.join(frontendRoot, 'src/components/FullPlayer.vue'))}
    import IptvHome from ${JSON.stringify(path.join(frontendRoot, 'src/views/IptvHome.vue'))}
    export { IptvHome }
    export default FullPlayer
  `)
  const fakePlugin = {
    name: 'full-player-test-media-mocks',
    enforce: 'pre',
    resolveId(id) {
      if (id === 'hls.js') return '\0full-player-test-hls'
      if (id === 'mpegts.js') return '\0full-player-test-mpegts'
    },
    load(id) {
      if (id === '\0full-player-test-hls') return fakeHlsModule()
      if (id === '\0full-player-test-mpegts') return fakeMpegtsModule()
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
      lib: {
        entry: tmpEntry,
        formats: ['es'],
        fileName: 'full-player-dom',
      },
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
  builtComponentPath = path.join(tmpDir, 'full-player-dom.js')
  const componentModule = await import(`${builtComponentPath}?dom-test=${Date.now()}`)
  mount = testUtils.mount
  flushPromises = testUtils.flushPromises
  createPinia = pinia.createPinia
  setActivePinia = pinia.setActivePinia
  usePlayerStore = (await import('../../src/stores/player.js')).usePlayerStore
  FullPlayer = componentModule.default
  IptvHome = componentModule.IptvHome
  vueRef = vueRuntime.ref
}

let channels
let fetchCalls
let fetchOverride

function installFetch() {
  fetchCalls = []
  globalThis.fetch = async (input, options = {}) => {
    const url = String(input?.url || input)
    fetchCalls.push(url)
    const overrideResult = await fetchOverride?.(url, options)
    if (overrideResult) return overrideResult
    if (url.includes('/api/iptv/channels')) {
      const parsed = new URL(url, window.location.origin)
      const group = parsed.searchParams.get('group') || ''
      const search = parsed.searchParams.get('search') || ''
      const filtered = channels.filter((item) => (
        (!group || item.group_name === group)
        && (!search || item.name.includes(search))
      ))
      return response({
        channels: filtered,
        groups: [...new Set(channels.map((item) => item.group_name).filter(Boolean))],
      })
    }
    if (url.includes('/api/iptv/epg/programs/')) {
      const date = new URL(url, window.location.origin).searchParams.get('date') || '2026-08-05'
      return response({
        current: { title: `${date}-current`, start: `${date}T10:00:00Z`, stop: `${date}T11:00:00Z` },
        next: null,
        programs: [{ title: `${date}-program`, start: `${date}T10:00:00Z`, stop: `${date}T11:00:00Z`, status: 'current' }],
        date,
        available_dates: ['2026-08-04', '2026-08-05', '2026-08-06'],
      })
    }
    if (url.includes('/api/iptv/epg/batch-current')) {
      const keys = JSON.parse(options.body || '{}').canonical_keys || []
      return response(Object.fromEntries(keys.map((key) => [key, null])))
    }
    return response({ ok: true })
  }
}

async function mountPlayer({ current = channels[0], expanded = true } = {}) {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.isPlayerExpanded = expanded
  store.activeMode = 'iptv'
  store.currentIptvChannel = current
  store.iptvUrls = current.urls
  store.iptvUrlIndex = 0
  store.isPlaying = true
  store.isLoading = false
  const wrapper = mount(FullPlayer, {
    attachTo: document.body,
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.vm.$nextTick()
  return { wrapper, store }
}

async function mountFullPlayerForStore(store, { expanded = true } = {}) {
  store.isPlayerExpanded = expanded
  store.activeMode = 'iptv'
  const wrapper = mount(FullPlayer, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.vm.$nextTick()
  return wrapper
}

async function mountHome({ store = null, search = '' } = {}) {
  if (!store) {
    setActivePinia(createPinia())
    store = usePlayerStore()
  }
  const searchQuery = vueRef(search)
  const scrollRef = vueRef(document.documentElement)
  const wrapper = mount(IptvHome, {
    attachTo: document.body,
    global: {
      provide: { searchQuery, scrollRef },
    },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.vm.$nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
  await wrapper.vm.$nextTick()
  return { wrapper, store, searchQuery }
}

async function mountHomePending({ store = null, search = '' } = {}) {
  if (!store) {
    setActivePinia(createPinia())
    store = usePlayerStore()
  }
  const searchQuery = vueRef(search)
  const scrollRef = vueRef(document.documentElement)
  const wrapper = mount(IptvHome, {
    attachTo: document.body,
    global: {
      provide: { searchQuery, scrollRef },
    },
  })
  mountedWrappers.push(wrapper)
  await wrapper.vm.$nextTick()
  return { wrapper, store, searchQuery }
}

function buttonByText(selector, text) {
  const buttons = domElements(selector)
  const button = buttons.find((item) => item.textContent.trim().includes(text))
  assert.ok(button, `找不到按钮: ${selector} / ${text}; 当前按钮=${buttons.map((item) => item.textContent.trim()).join('|')}`)
  return button
}

async function clickButtonByText(selector, text) {
  buttonByText(selector, text).dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await flushPromises()
}

function domElements(selector) {
  return [...document.querySelectorAll(selector)]
}

function domElement(selector, index = 0) {
  const element = domElements(selector)[index]
  assert.ok(element, `找不到真实 DOM 元素: ${selector}[${index}]`)
  return element
}

async function clickDom(selector, index = 0) {
  domElement(selector, index).dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await flushPromises()
}

function activeMediaCount() {
  return document.querySelectorAll('.media-video, .youtube-player-host.active iframe').length
}

function dispatchUiEvent(element, type) {
  element.dispatchEvent(new window.Event(type, { bubbles: true, cancelable: true }))
}

function installFullscreenMock() {
  let fullscreenElement = null
  const previousFullscreenElement = Object.getOwnPropertyDescriptor(document, 'fullscreenElement')
  const previousFullscreenEnabled = Object.getOwnPropertyDescriptor(document, 'fullscreenEnabled')
  const previousExitFullscreen = Object.getOwnPropertyDescriptor(document, 'exitFullscreen')
  const previousRequestFullscreen = Object.getOwnPropertyDescriptor(window.Element.prototype, 'requestFullscreen')
  Object.defineProperty(document, 'fullscreenEnabled', {
    configurable: true,
    value: true,
  })
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    get: () => fullscreenElement,
  })
  document.exitFullscreen = async () => {
    fullscreenElement = null
    document.dispatchEvent(new window.Event('fullscreenchange'))
  }
  Object.defineProperty(window.Element.prototype, 'requestFullscreen', {
    configurable: true,
    writable: true,
    async value() {
      if (this.__rejectFullscreenRequest) throw new Error('fullscreen rejected')
      fullscreenElement = this
      document.dispatchEvent(new window.Event('fullscreenchange'))
    },
  })
  return {
    attach(element, { reject = false } = {}) {
      element.__rejectFullscreenRequest = reject
    },
    restore() {
      if (previousFullscreenElement) Object.defineProperty(document, 'fullscreenElement', previousFullscreenElement)
      else delete document.fullscreenElement
      if (previousFullscreenEnabled) Object.defineProperty(document, 'fullscreenEnabled', previousFullscreenEnabled)
      else delete document.fullscreenEnabled
      if (previousExitFullscreen) Object.defineProperty(document, 'exitFullscreen', previousExitFullscreen)
      else delete document.exitFullscreen
      if (previousRequestFullscreen) Object.defineProperty(window.Element.prototype, 'requestFullscreen', previousRequestFullscreen)
      else delete window.Element.prototype.requestFullscreen
    },
  }
}

async function advanceOverlayTimers(milliseconds) {
  mock.timers.tick(milliseconds)
  await flushPromises()
}

async function settlePlayback() {
  await new Promise(resolve => setTimeout(resolve, 760))
  await flushPromises()
}

before(async () => {
  await loadComponent()
})

beforeEach(() => {
  document.body.innerHTML = '<div id="app"></div>'
  channels = [
    channel('Alpha', 'alpha'),
    channel('Bravo', 'bravo'),
    channel('Charlie', 'charlie'),
    channel('Not Live', 'not-live', { urls: [{ url: 'https://media.example/not-live.m3u8', source_id: 'not-live', source_type: 'hls', probe_status: 'not_live' }] }),
    channel('Disabled', 'disabled', { urls: [{ url: 'https://media.example/disabled.m3u8', source_id: 'disabled', source_type: 'hls', disabled: true }] }),
    channel('Unsupported', 'unsupported', { urls: [{ url: 'https://media.example/unsupported.xyz', source_id: 'unsupported', source_type: 'unsupported' }] }),
  ]
  installFetch()
  fetchOverride = null
})

afterEach(() => {
  for (const wrapper of mountedWrappers.splice(0)) {
    if (wrapper.exists()) wrapper.unmount()
  }
  mock.timers.reset()
  document.body.innerHTML = '<div id="app"></div>'
})

after(async () => {
  dom?.window.close()
})

function desktopChannelNames() {
  return domElements('.side-panel .channel-row .channel-title').map((element) => element.textContent.trim())
}

function mobileChannelNames() {
  return domElements('.mobile-panel .channel-row .channel-title').map((element) => element.textContent.trim())
}

function homeChannelNames() {
  return domElements('.channel-card').map((element) => String(element.getAttribute('aria-label') || '').replace(/^播放\s*/, ''))
}

test('IptvHome 从全部频道进入后 FullPlayer 继承全部集合和基础顺序', async () => {
  channels = [
    channel('Charlie', 'charlie', { group_name: '体育' }),
    channel('Alpha', 'alpha', { group_name: '央视' }),
    channel('Bravo', 'bravo', { group_name: '卫视' }),
  ]
  installFetch()
  const { store } = await mountHome()
  assert.deepEqual(homeChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  await clickDom('.channel-card', 0)
  assert.equal(store.iptvChannelContext.group, '')
  assert.equal(store.iptvChannelContext.search, '')
  assert.deepEqual(store.iptvChannelContext.channels.map((item) => item.name), ['Charlie', 'Alpha', 'Bravo'])
  await mountFullPlayerForStore(store)
  assert.deepEqual(desktopChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  assert.deepEqual(mobileChannelNames(), desktopChannelNames())
})

test('IptvHome 从分组进入后 FullPlayer 只继承该分组', async () => {
  channels = [
    channel('CCTV-1', 'cctv-1', { group_name: '央视' }),
    channel('CCTV-2', 'cctv-2', { group_name: '央视' }),
    channel('湖南卫视', 'hunan', { group_name: '卫视' }),
  ]
  installFetch()
  const { store } = await mountHome()
  await clickButtonByText('.tag-filter-row button, header button', '央视')
  assert.deepEqual(homeChannelNames(), ['CCTV-1', 'CCTV-2'])
  await clickDom('.channel-card', 0)
  assert.equal(store.iptvChannelContext.group, '央视')
  await mountFullPlayerForStore(store)
  assert.deepEqual(desktopChannelNames(), ['CCTV-1', 'CCTV-2'])
})

test('IptvHome 搜索以及分组加搜索的结果集合被 FullPlayer 原样继承', async () => {
  channels = [
    channel('福建新闻', 'fj-news', { group_name: '福建' }),
    channel('福建综合', 'fj-main', { group_name: '福建' }),
    channel('泉州新闻', 'qz-news', { group_name: '福建' }),
    channel('央视新闻', 'cctv-news', { group_name: '央视' }),
  ]
  installFetch()
  const first = await mountHome({ search: '新闻' })
  assert.deepEqual(homeChannelNames(), ['福建新闻', '泉州新闻', '央视新闻'])
  await clickDom('.channel-card', 0)
  assert.equal(first.store.iptvChannelContext.search, '新闻')
  await mountFullPlayerForStore(first.store)
  assert.deepEqual(desktopChannelNames(), ['福建新闻', '泉州新闻', '央视新闻'])

  for (const wrapper of mountedWrappers.splice(0)) {
    if (wrapper.exists()) wrapper.unmount()
  }
  document.body.innerHTML = '<div id="app"></div>'
  installFetch()
  const second = await mountHome()
  await clickButtonByText('.tag-filter-row button, header button', '福建')
  second.searchQuery.value = '新闻'
  await flushPromises()
  await second.wrapper.vm.$nextTick()
  assert.deepEqual(homeChannelNames(), ['福建新闻', '泉州新闻'])
  await clickDom('.channel-card', 0)
  assert.equal(second.store.iptvChannelContext.group, '福建')
  assert.equal(second.store.iptvChannelContext.search, '新闻')
  await mountFullPlayerForStore(second.store)
  assert.deepEqual(desktopChannelNames(), ['福建新闻', '泉州新闻'])
})

test('IptvHome 将初始 loading 和成功目录投影为明确状态', async () => {
  channels = [channel('Alpha', 'alpha')]
  let resolveCatalog
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    return new Promise((resolve) => { resolveCatalog = resolve })
  }
  installFetch()
  const home = await mountHomePending()

  assert.equal(domElement('[data-iptv-catalog-grid]').getAttribute('aria-busy'), 'true')
  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /正在加载频道目录/)

  resolveCatalog(response({ channels, groups: ['测试'] }))
  await flushPromises()
  await home.wrapper.vm.$nextTick()

  assert.equal(domElement('[data-iptv-catalog-grid]').getAttribute('aria-busy'), 'false')
  assert.deepEqual(homeChannelNames(), ['Alpha'])
  assert.equal(domElements('[data-iptv-catalog-empty-state]').length, 0)
})

test('IptvHome 区分合法空目录与筛选无结果', async () => {
  channels = []
  installFetch()
  const empty = await mountHome()
  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /暂无可用频道/)
  assert.equal(domElements('[data-iptv-catalog-retry]').length, 0)

  empty.searchQuery.value = '不存在'
  await flushPromises()
  await empty.wrapper.vm.$nextTick()
  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /没有匹配的频道/)
  assert.equal(domElements('[data-iptv-catalog-empty-state] button').length, 1)
})

test('IptvHome 清除仅分组筛选会重新加载全量目录', async () => {
  channels = [channel('Alpha', 'alpha', { group_name: 'A' })]
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    const group = new URL(url, window.location.origin).searchParams.get('group') || ''
    if (group === '空组') return response({ channels: [], groups: ['A', '空组'] })
    return response({ channels, groups: ['A', '空组'] })
  }
  installFetch()
  const home = await mountHome()

  await clickButtonByText('.tag-filter-row button', '空组')
  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /没有匹配的频道/)
  const requestsBeforeClear = fetchCalls.filter((url) => url.includes('/api/iptv/channels')).length

  await clickDom('[data-iptv-catalog-empty-state] button')
  await home.wrapper.vm.$nextTick()
  assert.deepEqual(homeChannelNames(), ['Alpha'])
  assert.equal(fetchCalls.filter((url) => url.includes('/api/iptv/channels')).length, requestsBeforeClear + 1)
})

test('IptvHome API 失败显示错误状态，重试成功后恢复目录', async () => {
  channels = [channel('Alpha', 'alpha')]
  let failed = true
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    if (failed) return response({}, { ok: false, status: 503, json: async () => ({ detail: 'upstream unavailable' }) })
    return response({ channels, groups: ['测试'] })
  }
  installFetch()
  const home = await mountHome()

  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /频道目录暂时无法加载/)
  assert.equal(domElements('[data-iptv-catalog-retry]').length, 1)

  failed = false
  await clickDom('[data-iptv-catalog-retry]')
  await flushPromises()
  await home.wrapper.vm.$nextTick()
  assert.deepEqual(homeChannelNames(), ['Alpha'])
  assert.equal(domElements('[data-iptv-catalog-empty-state]').length, 0)
})

test('IptvHome 请求超时进入错误状态，不被误投影为取消或空目录', async () => {
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    throw Object.assign(new Error('请求超时'), { status: 0 })
  }
  installFetch()
  await mountHome()

  assert.match(domElement('[data-iptv-catalog-empty-state]').textContent, /频道目录暂时无法加载/)
  assert.equal(domElements('[data-iptv-catalog-retry]').length, 1)
})

test('IptvHome 刷新失败保留 last-known-good 目录并标记 stale', async () => {
  channels = [channel('Alpha', 'alpha')]
  installFetch()
  const home = await mountHome()
  assert.deepEqual(homeChannelNames(), ['Alpha'])

  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    return response({}, { ok: false, status: 504, json: async () => ({ detail: 'timeout' }) })
  }
  home.searchQuery.value = 'Alpha'
  await flushPromises()
  await home.wrapper.vm.$nextTick()

  assert.deepEqual(homeChannelNames(), ['Alpha'])
  assert.match(domElement('.iptv-catalog-notice').textContent, /已保留上次频道/)
  assert.equal(domElement('[data-iptv-catalog-grid]').getAttribute('aria-busy'), 'false')
})

test('IptvHome A/B 目录请求按 latest-wins 投影', async () => {
  const replies = []
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/channels')) return null
    return new Promise((resolve) => replies.push(resolve))
  }
  installFetch()
  const home = await mountHomePending()
  home.searchQuery.value = 'B'
  await home.wrapper.vm.$nextTick()
  await flushPromises()
  assert.equal(replies.length, 2)

  replies[1](response({ channels: [channel('Bravo', 'bravo')], groups: ['测试'] }))
  await flushPromises()
  await home.wrapper.vm.$nextTick()
  replies[0](response({ channels: [channel('Alpha', 'alpha')], groups: ['测试'] }))
  await flushPromises()
  await home.wrapper.vm.$nextTick()

  assert.deepEqual(homeChannelNames(), ['Bravo'])
  assert.equal(domElement('[data-iptv-catalog-grid]').getAttribute('aria-busy'), 'false')
})

test('IptvHome 同名频道按 stable canonical identity 选中，不串台', async () => {
  channels = [
    channel('同名频道', 'same-a', { canonical_key: 'same-a' }),
    channel('同名频道', 'same-b', { canonical_key: 'same-b' }),
  ]
  installFetch()
  const { store } = await mountHome()
  const cards = domElements('.channel-card')
  assert.equal(cards.length, 2)
  assert.equal(cards[0].getAttribute('data-canonical-key'), 'same-a')
  assert.equal(cards[1].getAttribute('data-canonical-key'), 'same-b')

  cards[1].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.canonical_key, 'same-b')
  assert.equal(domElements('.channel-card.channel-card-current').length, 1)
  assert.equal(domElement('.channel-card.channel-card-current').getAttribute('data-canonical-key'), 'same-b')
})

test('首页和 FullPlayer 共用排序状态，默认基础顺序与双向切换保持一致', async () => {
  channels = [
    channel('Charlie', 'charlie', { group_name: '卫视' }),
    channel('Alpha', 'alpha', { group_name: '央视' }),
    channel('Bravo', 'bravo', { group_name: '央视' }),
  ]
  installFetch()
  const { store } = await mountHome()
  assert.equal(store.iptvChannelSortMode, 'original')
  assert.match(buttonByText('.iptv-main header button', '默认排序').textContent, /默认排序/)
  assert.deepEqual(homeChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  await clickButtonByText('.iptv-main header button', '默认排序')
  assert.equal(store.iptvChannelSortMode, 'natural')
  assert.deepEqual(homeChannelNames(), ['Alpha', 'Bravo', 'Charlie'])
  await clickDom('.channel-card', 0)
  await mountFullPlayerForStore(store)
  assert.deepEqual(desktopChannelNames(), ['Alpha', 'Bravo', 'Charlie'])
  assert.match(domElement('.side-panel .sort-btn').textContent, /A-Z排序/)
  await clickDom('.side-panel .sort-btn')
  assert.equal(store.iptvChannelSortMode, 'group')
  assert.deepEqual(desktopChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  assert.deepEqual(homeChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  assert.match(domElement('.side-panel .sort-btn').textContent, /分组排序/)
  await clickDom('.side-panel .sort-btn')
  assert.equal(store.iptvChannelSortMode, 'original')
  assert.deepEqual(desktopChannelNames(), ['Charlie', 'Alpha', 'Bravo'])
  assert.doesNotMatch(document.body.textContent, /直播中排序/)
  store.setIptvChannelSortMode('group')
  await flushPromises()
  await clickDom('[aria-label="下一个"]')
  assert.equal(store.currentIptvChannel.name, 'Bravo')
})

test('IptvHome density toggle defaults compact on mobile, persists selection, and keeps card clicks', async () => {
  const storageKey = 'waveflow.iptv.card-density'
  const originalWidth = window.innerWidth
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
  window.localStorage.removeItem(storageKey)
  channels = [
    channel('Alpha', 'alpha'),
    channel('Not Live', 'not-live', { urls: [{ url: 'https://media.example/not-live.m3u8', source_id: 'not-live', source_type: 'hls', probe_status: 'not_live' }] }),
  ]
  installFetch()

  try {
    const { wrapper, store } = await mountHome()
    assert.equal(domElement('.iptv-main').classList.contains('iptv-density-compact'), true)
    assert.equal(domElements('.channel-card .card-info').length, 0)
    assert.equal(domElements('.channel-card .card-program-name').length, 0)
    assert.equal(domElements('.channel-card__compact-status').length, 1, 'Compact 只保留异常状态指示')
    assert.equal(domElement('.channel-card__compact-status').getAttribute('aria-label'), '未开播')

    await clickDom('.channel-card', 0)
    assert.equal(store.currentIptvChannel.name, 'Alpha')
    assert.equal(domElement('.channel-card').getAttribute('data-canonical-key'), 'alpha')

    await clickDom('[data-density-toggle]')
    assert.equal(domElement('.iptv-main').classList.contains('iptv-density-compact'), false)
    assert.equal(domElements('.channel-card .card-info').length, 2)
    assert.equal(window.localStorage.getItem(storageKey), 'standard')

    wrapper.unmount()
    document.body.innerHTML = '<div id="app"></div>'
    await mountHome()
    assert.equal(domElement('.iptv-main').classList.contains('iptv-density-compact'), false)
  } finally {
    window.localStorage.removeItem(storageKey)
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
  }
})

test('FullPlayer and shared player store keep mute state synchronized', async () => {
  channels = [channel('Alpha', 'alpha')]
  installFetch()
  const { store } = await mountPlayer()

  assert.equal(store.isMuted, false)
  assert.equal(domElement('[aria-label="静音"]').getAttribute('aria-label'), '静音')

  await clickDom('[aria-label="静音"]')
  assert.equal(store.isMuted, true)
  assert.equal(domElement('[aria-label="取消静音"]').getAttribute('aria-label'), '取消静音')

  await clickDom('[aria-label="取消静音"]')
  assert.equal(store.isMuted, false)
  assert.equal(domElement('[aria-label="静音"]').getAttribute('aria-label'), '静音')
})

test('FullPlayer desktop volume panel mutes at zero and restores the last non-zero volume', async () => {
  channels = [channel('Alpha', 'alpha')]
  installFetch()
  const { store } = await mountPlayer()
  const slider = domElement('.desktop-video-overlay .video-overlay-volume-panel input[type="range"]')

  slider.value = '0'
  slider.dispatchEvent(new window.Event('input', { bubbles: true }))
  await flushPromises()
  assert.equal(store.volume, 0)
  assert.equal(store.isMuted, true)
  assert.equal(domElement('.desktop-video-overlay .video-overlay-volume-panel button').getAttribute('aria-label'), '取消静音')

  slider.value = '0.42'
  slider.dispatchEvent(new window.Event('input', { bubbles: true }))
  await flushPromises()
  assert.equal(store.volume, 0.42)
  assert.equal(store.isMuted, false)

  await clickDom('.desktop-video-overlay .video-overlay-volume-panel button')
  assert.equal(store.isMuted, true)
  await clickDom('.desktop-video-overlay .video-overlay-volume-panel button')
  assert.equal(store.isMuted, false)
  assert.equal(store.volume, 0.42)
})

test('FullPlayer desktop keyboard shortcuts reuse the overlay capture path', async () => {
  channels = [channel('Alpha', 'alpha'), channel('Bravo', 'bravo')]
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  try {
    const { store } = await mountPlayer({ current: channels[0] })
    const root = domElement('.full-player')
    const dispatchKey = (key, code = key) => {
      const event = new window.KeyboardEvent('keydown', {
        key,
        code,
        bubbles: true,
        cancelable: true,
      })
      root.dispatchEvent(event)
      return event
    }

    assert.equal(store.isPlaying, true)
    assert.equal(dispatchKey(' ', 'Space').defaultPrevented, true)
    assert.equal(store.isPlaying, false)
    dispatchKey('k', 'KeyK')
    assert.equal(store.isPlaying, true)

    store.setVolume(0.5)
    store.setMuted(false)
    dispatchKey('ArrowUp', 'ArrowUp')
    assert.equal(store.volume, 0.55)
    dispatchKey('ArrowDown', 'ArrowDown')
    assert.equal(store.volume, 0.5)

    dispatchKey('m', 'KeyM')
    assert.equal(store.isMuted, true)
    dispatchKey('m', 'KeyM')
    assert.equal(store.isMuted, false)

    dispatchKey('PageDown', 'PageDown')
    await flushPromises()
    assert.equal(store.currentIptvChannel.name, 'Bravo')

    store.isPlaying = true
    const slider = domElement('.desktop-video-overlay .video-overlay-volume-panel input[type="range"]')
    const ignored = new window.KeyboardEvent('keydown', {
      key: ' ',
      code: 'Space',
      bubbles: true,
      cancelable: true,
    })
    slider.dispatchEvent(ignored)
    assert.equal(ignored.defaultPrevented, false)
    assert.equal(store.isPlaying, true)
  } finally {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer pointer command controls return focus to the player root before shortcuts', async () => {
  channels = [channel('Alpha', 'alpha'), channel('Bravo', 'bravo')]
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })

  try {
    const { store } = await mountPlayer({ current: channels[0] })
    const root = domElement('.full-player')
    const muteButton = domElement('.desktop-video-overlay [aria-label="静音"]')

    dispatchUiEvent(muteButton, 'pointerdown')
    muteButton.focus()
    muteButton.click()
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 0))

    assert.equal(store.isMuted, true)
    assert.equal(document.activeElement, root)

    const event = new window.KeyboardEvent('keydown', {
      key: ' ',
      code: 'Space',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(event)
    assert.equal(event.defaultPrevented, true)
    assert.equal(store.isPlaying, false)
  } finally {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer pointer command controls keep global M/PageDown/F shortcuts available', async () => {
  channels = [channel('Alpha', 'alpha'), channel('Bravo', 'bravo'), channel('Charlie', 'charlie')]
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  const fullscreen = installFullscreenMock()

  try {
    const { store } = await mountPlayer({ current: channels[0] })
    const root = domElement('.full-player')
    const media = domElement('.media-card')
    const railButton = domElement('.desktop-video-overlay button[aria-label*="频道列表"]')
    fullscreen.attach(media)

    dispatchUiEvent(railButton, 'pointerdown')
    railButton.focus()
    railButton.click()
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(document.activeElement, root)

    const mute = new window.KeyboardEvent('keydown', {
      key: 'm',
      code: 'KeyM',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(mute)
    assert.equal(mute.defaultPrevented, true)
    assert.equal(store.isMuted, true)

    const next = new window.KeyboardEvent('keydown', {
      key: 'PageDown',
      code: 'PageDown',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(next)
    await flushPromises()
    assert.equal(next.defaultPrevented, true)
    assert.equal(store.currentIptvChannel.name, 'Bravo')

    const fullscreenKey = new window.KeyboardEvent('keydown', {
      key: 'f',
      code: 'KeyF',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(fullscreenKey)
    await flushPromises()
    assert.equal(fullscreenKey.defaultPrevented, true)
    assert.equal(document.fullscreenElement, media)
  } finally {
    fullscreen.restore()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer keyboard-focused command, source trigger, and volume slider keep their native focus semantics', async () => {
  const current = channel('Alpha', 'alpha', {
    urls: [
      ...channel('Alpha', 'alpha').urls,
      { url: 'https://media.example/alpha-backup.m3u8', source_id: 'alpha-backup', source_type: 'hls', probe_status: 'online', is_working: 1 },
    ],
  })
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })

  try {
    const { store } = await mountPlayer({ current })
    const root = domElement('.full-player')
    const muteButton = domElement('.desktop-video-overlay [aria-label="静音"]')
    muteButton.focus()
    const keyboardEvent = new window.KeyboardEvent('keydown', {
      key: ' ',
      code: 'Space',
      bubbles: true,
      cancelable: true,
    })
    muteButton.dispatchEvent(keyboardEvent)
    assert.equal(keyboardEvent.defaultPrevented, false)
    assert.equal(store.isPlaying, true)

    const sourceButton = domElement('.desktop-video-overlay [aria-label="切换播放源"]')
    dispatchUiEvent(sourceButton, 'pointerdown')
    sourceButton.focus()
    sourceButton.click()
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(document.activeElement, sourceButton)
    assert.equal(domElement('#iptv-source-menu').getAttribute('style').includes('display: none'), false)

    const slider = domElement('.desktop-video-overlay .video-overlay-volume-panel input[type="range"]')
    dispatchUiEvent(slider, 'pointerdown')
    slider.focus()
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(document.activeElement, slider)

    root.focus()
  } finally {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer pointer fullscreen entry moves residual trigger focus inside the fullscreen shortcut scope', async () => {
  channels = [channel('Alpha', 'alpha'), channel('Bravo', 'bravo')]
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  const fullscreen = installFullscreenMock()

  try {
    const { store } = await mountPlayer({ current: channels[0] })
    const media = domElement('.media-card')
    const trigger = domElement('.desktop-video-overlay [aria-label="全屏"]')
    const playButton = domElement('.desktop-video-overlay [aria-label="暂停"]')
    fullscreen.attach(media)

    trigger.dispatchEvent(new window.Event('pointerdown', { bubbles: true, cancelable: true }))
    trigger.focus()
    trigger.click()
    await flushPromises()

    assert.equal(document.fullscreenElement, media)
    assert.equal(trigger.getAttribute('aria-pressed'), 'true')
    assert.equal(document.activeElement, media)

    dispatchUiEvent(playButton, 'pointerdown')
    playButton.focus()
    playButton.click()
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(store.isPlaying, false)
    assert.equal(document.activeElement, media)

    const dispatchKey = (key, code = key) => {
      const event = new window.KeyboardEvent('keydown', {
        key,
        code,
        bubbles: true,
        cancelable: true,
      })
      document.activeElement.dispatchEvent(event)
      return event
    }
    assert.equal(dispatchKey(' ', 'Space').defaultPrevented, true)
    assert.equal(store.isPlaying, true)
    assert.equal(dispatchKey('m', 'KeyM').defaultPrevented, true)
    assert.equal(store.isMuted, true)
    assert.equal(dispatchKey('ArrowUp', 'ArrowUp').defaultPrevented, true)
    assert.equal(store.volume, 0.05)
    assert.equal(dispatchKey('PageDown', 'PageDown').defaultPrevented, true)
    await flushPromises()
    assert.equal(store.currentIptvChannel.name, 'Bravo')
  } finally {
    fullscreen.restore()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer keyboard-focused fullscreen control keeps ordinary shortcuts behind the interactive guard', async () => {
  installFetch()
  const fullscreen = installFullscreenMock()
  try {
    const { store } = await mountPlayer()
    const media = domElement('.media-card')
    const trigger = domElement('.desktop-video-overlay [aria-label="全屏"]')
    fullscreen.attach(media)
    trigger.focus()

    const activation = new window.KeyboardEvent('keydown', {
      key: 'Enter',
      code: 'Enter',
      bubbles: true,
      cancelable: true,
    })
    trigger.dispatchEvent(activation)
    trigger.click()
    await flushPromises()

    assert.equal(activation.defaultPrevented, false)
    assert.equal(document.fullscreenElement, media)
    assert.equal(document.activeElement, trigger)

    const event = new window.KeyboardEvent('keydown', {
      key: 'm',
      code: 'KeyM',
      bubbles: true,
      cancelable: true,
    })
    trigger.dispatchEvent(event)

    assert.equal(event.defaultPrevented, false)
    assert.equal(store.isMuted, false)
  } finally {
    fullscreen.restore()
  }
})

test('FullPlayer F shortcut enters and exits fullscreen without pointer focus transfer', async () => {
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  const fullscreen = installFullscreenMock()
  try {
    const { store } = await mountPlayer()
    const root = domElement('.full-player')
    const media = domElement('.media-card')
    fullscreen.attach(media)
    root.focus()

    const enter = new window.KeyboardEvent('keydown', {
      key: 'f',
      code: 'KeyF',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(enter)
    await flushPromises()
    assert.equal(enter.defaultPrevented, true)
    assert.equal(document.fullscreenElement, media)
    assert.equal(document.activeElement, media)

    const exit = new window.KeyboardEvent('keydown', {
      key: 'f',
      code: 'KeyF',
      bubbles: true,
      cancelable: true,
    })
    media.dispatchEvent(exit)
    await flushPromises()
    assert.equal(exit.defaultPrevented, true)
    assert.equal(document.fullscreenElement, null)
    assert.equal(document.activeElement, root)
    assert.equal(store.isPlayerExpanded, true)
  } finally {
    fullscreen.restore()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer unsupported fullscreen capability fails closed for pointer and F shortcut entry', async () => {
  installFetch()
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  try {
    const { store } = await mountPlayer()
    const root = domElement('.full-player')
    const trigger = domElement('.desktop-video-overlay [aria-label="全屏"]')

    assert.equal(trigger.getAttribute('aria-pressed'), 'false')
    trigger.click()
    await flushPromises()
    assert.equal(Boolean(document.fullscreenElement), false)
    assert.equal(trigger.getAttribute('aria-pressed'), 'false')

    root.focus()
    const event = new window.KeyboardEvent('keydown', {
      key: 'f',
      code: 'KeyF',
      bubbles: true,
      cancelable: true,
    })
    root.dispatchEvent(event)
    await flushPromises()
    assert.equal(event.defaultPrevented, true)
    assert.equal(Boolean(document.fullscreenElement), false)
    assert.equal(trigger.getAttribute('aria-pressed'), 'false')
    assert.equal(store.isPlayerExpanded, true)
  } finally {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer rejected fullscreen request leaves the platform state and aria state false', async () => {
  installFetch()
  const fullscreen = installFullscreenMock()
  try {
    const { store } = await mountPlayer()
    const media = domElement('.media-card')
    const trigger = domElement('.desktop-video-overlay [aria-label="全屏"]')
    fullscreen.attach(media, { reject: true })

    trigger.click()
    await flushPromises()

    assert.equal(document.fullscreenElement, null)
    assert.equal(trigger.getAttribute('aria-pressed'), 'false')
    assert.equal(store.isPlayerExpanded, true)
  } finally {
    fullscreen.restore()
  }
})

test('FullPlayer desktop activity is scoped to media surface and rail opening does not pin it', async () => {
  const originalWidth = window.innerWidth
  const originalMatchMedia = window.matchMedia
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
  window.matchMedia = (query) => ({
    matches: query.includes('hover: hover') || query.includes('pointer: fine'),
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
  })
  mock.timers.enable({ apis: ['setTimeout'] })

  try {
    await mountPlayer()
    await advanceOverlayTimers(0)

    const root = domElement('.full-player')
    const layout = domElement('.player-layout')
    const media = domElement('.media-card')
    const rail = domElement('.side-panel')
    const overlay = domElement('.desktop-video-overlay')
    const railControl = rail.querySelector('button')

    await advanceOverlayTimers(2800)
    assert.equal(overlay.classList.contains('is-hidden'), true)

    root.dispatchEvent(new window.KeyboardEvent('keydown', {
      key: 'Tab',
      code: 'Tab',
      bubbles: true,
      cancelable: true,
    }))
    railControl.focus()
    await flushPromises()
    assert.equal(overlay.classList.contains('is-hidden'), true)

    dispatchUiEvent(layout, 'pointermove')
    dispatchUiEvent(root, 'pointermove')
    dispatchUiEvent(rail, 'pointermove')
    assert.equal(overlay.classList.contains('is-hidden'), true)

    dispatchUiEvent(media, 'pointermove')
    await flushPromises()
    assert.equal(overlay.classList.contains('is-hidden'), false)

    const railToggle = domElements('.desktop-video-overlay [aria-label="显示频道列表"], .desktop-video-overlay [aria-label="隐藏频道列表"]')[0]
    assert.ok(railToggle)
    railToggle.click()
    await flushPromises()
    assert.equal(overlay.classList.contains('is-hidden'), false)
    await advanceOverlayTimers(2800)
    assert.equal(overlay.classList.contains('is-hidden'), true)

    dispatchUiEvent(media, 'pointermove')
    dispatchUiEvent(media, 'pointerleave')
    await advanceOverlayTimers(900)
    assert.equal(overlay.classList.contains('is-hidden'), true)
  } finally {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    window.matchMedia = originalMatchMedia
  }
})

test('FullPlayer mobile panel pointer/touch/scroll does not wake a hidden overlay', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  await mountPlayer()
  await advanceOverlayTimers(0)

  const overlay = domElement('.mobile-video-overlay')
  const panel = domElement('.mobile-panel')
  const media = domElement('.media-card')

  await advanceOverlayTimers(2800)
  assert.equal(overlay.classList.contains('is-hidden'), true)

  dispatchUiEvent(panel, 'pointermove')
  dispatchUiEvent(panel, 'touchstart')
  dispatchUiEvent(panel, 'scroll')
  assert.equal(overlay.classList.contains('is-hidden'), true)

  dispatchUiEvent(media, 'pointermove')
  await flushPromises()
  assert.equal(overlay.classList.contains('is-hidden'), false)
})

test('FullPlayer keyboard focus pins media controls, panel focus does not, and pointer focus can auto-hide', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  await mountPlayer()
  await advanceOverlayTimers(0)

  const root = domElement('.full-player')
  const overlay = domElement('.mobile-video-overlay')
  const panelTab = domElement('.mobile-panel-header .panel-tabs button')
  const muteButton = domElement('.mobile-video-overlay [aria-label="静音"]')

  await advanceOverlayTimers(2800)
  assert.equal(overlay.classList.contains('is-hidden'), true)

  root.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'Tab',
    code: 'Tab',
    bubbles: true,
    cancelable: true,
  }))
  panelTab.focus()
  await flushPromises()
  assert.equal(overlay.classList.contains('is-hidden'), true)

  root.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'Tab',
    code: 'Tab',
    bubbles: true,
    cancelable: true,
  }))
  muteButton.focus()
  await flushPromises()
  assert.equal(overlay.classList.contains('is-hidden'), false)
  await advanceOverlayTimers(5000)
  assert.equal(overlay.classList.contains('is-hidden'), false)

  dispatchUiEvent(muteButton, 'pointerdown')
  muteButton.focus()
  muteButton.click()
  await flushPromises()
  await advanceOverlayTimers(2800)
  assert.equal(overlay.classList.contains('is-hidden'), true)
})

test('FullPlayer source popup stays pinned and resumes hide timing after close', async () => {
  const current = channel('Alpha', 'alpha', {
    urls: [
      ...channel('Alpha', 'alpha').urls,
      { url: 'https://media.example/alpha-backup.m3u8', source_id: 'alpha-backup', source_type: 'hls', probe_status: 'online', is_working: 1 },
    ],
  })
  mock.timers.enable({ apis: ['setTimeout'] })
  await mountPlayer({ current })
  await advanceOverlayTimers(0)

  const overlay = domElement('.mobile-video-overlay')
  const sourceButton = domElement('.mobile-video-overlay [aria-label="切换播放源"]')

  await advanceOverlayTimers(2800)
  assert.equal(overlay.classList.contains('is-hidden'), true)

  sourceButton.click()
  await flushPromises()
  assert.equal(domElement('#iptv-source-menu').getAttribute('style').includes('display: none'), false)
  await advanceOverlayTimers(5000)
  assert.equal(overlay.classList.contains('is-hidden'), false)

  sourceButton.click()
  await flushPromises()
  await advanceOverlayTimers(900)
  assert.equal(overlay.classList.contains('is-hidden'), true)
})

test('FullPlayer loading indicator is delayed, follows loading state, and is hidden when paused', async () => {
  channels = [channel('Alpha', 'alpha')]
  installFetch()
  const { wrapper, store } = await mountPlayer()

  assert.equal(domElements('.video-loading-indicator').length, 0)
  store.isLoading = true
  await new Promise((resolve) => setTimeout(resolve, 120))
  await wrapper.vm.$nextTick()
  assert.equal(domElements('.video-loading-indicator').length, 0)
  await new Promise((resolve) => setTimeout(resolve, 160))
  await wrapper.vm.$nextTick()
  assert.equal(domElements('.video-loading-indicator').length, 1)

  store.isLoading = false
  await flushPromises()
  assert.equal(domElements('.video-loading-indicator').length, 0)

  store.isPlaying = false
  store.isLoading = false
  await new Promise((resolve) => setTimeout(resolve, 280))
  await wrapper.vm.$nextTick()
  assert.equal(domElements('.video-loading-indicator').length, 0)
})

test('同名不同 canonical identity 只有当前频道行 active', async () => {
  channels = [
    channel('同名频道', 'same-a', { canonical_key: 'same-a' }),
    channel('同名频道', 'same-b', { canonical_key: 'same-b' }),
  ]
  installFetch()
  const { store } = await mountPlayer({ current: channels[1] })
  store.setIptvChannelContext({ channels })
  await flushPromises()
  const rows = domElements('.side-panel .channel-row')
  assert.equal(rows.length, 2)
  assert.equal(rows.filter((row) => row.classList.contains('active')).length, 1)
  assert.equal(rows[1].classList.contains('active'), true)
})

test('上下文刷新移除旧频道并加入新频道，当前被删除频道只临时置顶', async () => {
  const alpha = channel('Alpha', 'alpha')
  const bravo = channel('Bravo', 'bravo')
  const charlie = channel('Charlie', 'charlie')
  channels = [bravo, charlie]
  installFetch()
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.currentIptvChannel = alpha
  store.iptvUrls = alpha.urls
  store.isPlaying = true
  store.setIptvChannelContext({ channels: [alpha, bravo] })
  await mountFullPlayerForStore(store)
  assert.deepEqual(desktopChannelNames(), ['Alpha', 'Bravo', 'Charlie'])
  await clickDom('.side-panel .channel-row', 1)
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  assert.deepEqual(desktopChannelNames(), ['Bravo', 'Charlie'])
})

test('无首页上下文时按当前频道分组兜底，分组不存在时回退全量', async () => {
  const cctv = channel('CCTV-1', 'cctv', { group_name: '央视' })
  const hunan = channel('湖南卫视', 'hunan', { group_name: '卫视' })
  channels = [cctv, hunan]
  installFetch()
  const first = await mountPlayer({ current: cctv })
  assert.equal(first.store.iptvChannelContext, null)
  assert.deepEqual(desktopChannelNames(), ['CCTV-1'])

  for (const wrapper of mountedWrappers.splice(0)) {
    if (wrapper.exists()) wrapper.unmount()
  }
  document.body.innerHTML = '<div id="app"></div>'
  const missing = channel('临时频道', 'temporary', { group_name: '不存在分组' })
  installFetch()
  await mountPlayer({ current: missing })
  assert.deepEqual(desktopChannelNames(), ['临时频道', 'CCTV-1', '湖南卫视'])
})

test('桌面频道行点击 A→B，使用真实模板且只保留当前频道', async () => {
  const { wrapper, store } = await mountPlayer()
  const rows = domElements('.side-panel .channel-row')
  assert.equal(rows.length, channels.length)
  rows[1].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  assert.equal(store.pendingIptvChannel, null)
  assert.equal(domElements('.side-panel .channel-row')[1].classList.contains('active'), true)
  assert.equal(store.iptvSelectionToken > 0, true)
  assert.equal(activeMediaCount(), 1)
})

test('移动端频道行点击 A→B 与桌面端共用选择入口', async () => {
  const { wrapper, store } = await mountPlayer()
  const rows = domElements('.mobile-panel .channel-row')
  assert.equal(rows.length, channels.length)
  rows[1].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  assert.equal(domElements('.mobile-panel .channel-row')[1].classList.contains('active'), true)
  assert.equal(activeMediaCount(), 1)
})

test('快速点击 A→B→C 最终只留下 C，上一台/下一台按可见顺序切换', async () => {
  const { wrapper, store } = await mountPlayer()
  const rows = domElements('.side-panel .channel-row')
  rows[0].click()
  rows[1].click()
  rows[2].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Charlie')
  assert.equal(store.pendingIptvChannel, null)
  await clickDom('[aria-label="上一个"]')
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  await clickDom('[aria-label="下一个"]')
  assert.equal(store.currentIptvChannel.name, 'Charlie')
  assert.equal(activeMediaCount(), 1)
})

test('排序变化后上一台/下一台按当前可见频道顺序切换', async () => {
  channels = [channels[2], channels[0], channels[1], ...channels.slice(3)]
  installFetch()
  const { store } = await mountPlayer({ current: channels[1] })
  await clickDom('.side-panel .sort-btn')
  const visibleNames = domElements('.side-panel .channel-row .channel-title')
    .map(element => element.textContent.trim())
  assert.deepEqual(visibleNames.slice(0, 3), ['Alpha', 'Bravo', 'Charlie'])
  await clickDom('[aria-label="下一个"]')
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  await clickDom('[aria-label="上一个"]')
  assert.equal(store.currentIptvChannel.name, 'Alpha')
})

test('快速 A→B→C 只有最终频道进入正式起播', async () => {
  globalThis.__fullPlayerHlsCalls = []
  const { store } = await mountPlayer()
  const rows = domElements('.side-panel .channel-row')
  rows[0].click()
  rows[1].click()
  rows[2].click()
  await settlePlayback()
  assert.equal(store.currentIptvChannel.name, 'Charlie')
  const formalStarts = (globalThis.__fullPlayerHlsCalls || []).filter(item => item.formal)
  assert.equal(formalStarts.length, 1)
  assert.match(formalStarts[0].url, /charlie\.m3u8$/)
})

test('not_live 可点击，显式 disabled 和 unsupported 不可点击', async () => {
  const { wrapper, store } = await mountPlayer()
  const rows = domElements('.side-panel .channel-row')
  rows[3].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Not Live')
  rows[4].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Not Live')
  assert.equal(rows[4].classList.contains('disabled'), true)
  rows[5].click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Not Live')
  assert.equal(rows[5].classList.contains('disabled'), true)
})

test('混合 supported + unsupported 频道仍可点击并使用受支持 source', async () => {
  const mixed = channel('Mixed', 'mixed-supported', {
    urls: [
      { url: 'https://media.example/mixed.xyz', source_id: 'mixed-unsupported', source_type: 'unsupported' },
      { url: 'https://media.example/mixed-supported.m3u8', source_id: 'mixed-supported', source_type: 'hls', probe_status: 'online', is_working: 1 },
    ],
  })
  channels = [channels[0], mixed, ...channels.slice(1)]
  installFetch()
  const { store } = await mountPlayer()
  const mixedRow = domElements('.side-panel .channel-row')[1]
  assert.equal(mixedRow.classList.contains('disabled'), false)
  mixedRow.click()
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Mixed')
  assert.equal(store.iptvUrls.some(entry => entry.source_id === 'mixed-supported'), true)
})

test('播放源菜单切换 direct→proxy→direct，active source 与 store 一致', async () => {
  const sourceA = { url: 'https://media.example/direct.m3u8', source_id: 'direct', source_type: 'hls', type: 'direct', recommended_display_label: '福建联通 · 1080P', probe_status: 'online', is_working: 1 }
  const sourceB = { url: 'http://localhost:5173/api/media/channel/test/playlist.m3u8', source_id: 'proxy', source_type: 'hls', type: 'proxy', via_proxy: true, recommended_display_label: 'mzky · 4K', probe_status: 'online', is_working: 1 }
  const current = { ...channels[0], urls: [sourceA, sourceB] }
  const { wrapper, store } = await mountPlayer({ current })
  await clickDom('[aria-label="切换播放源"]')
  const options = domElements('#iptv-source-menu button')
  assert.equal(options.length, 2)
  assert.match(options[0].textContent, /福建联通 · 1080P/)
  assert.match(options[1].textContent, /mzky · 4K/)
  options[1].click()
  await flushPromises()
  assert.equal(store.iptvUrls[store.iptvUrlIndex].source_id, 'proxy')
  await clickDom('[aria-label="切换播放源"]')
  domElements('#iptv-source-menu button')[0].click()
  await flushPromises()
  assert.equal(store.iptvUrls[store.iptvUrlIndex].source_id, 'direct')
})

test('FullPlayer 主区域显示 current、时间进度和轻量 next', async () => {
  const now = Date.now()
  const current = {
    title: '正在播出的特别节目名称',
    start: new Date(now - 10 * 60_000).toISOString(),
    stop: new Date(now + 20 * 60_000).toISOString(),
  }
  const next = {
    title: '下一档新闻',
    start: new Date(now + 20 * 60_000).toISOString(),
    stop: new Date(now + 50 * 60_000).toISOString(),
  }
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return response({
      current,
      next,
      programs: [
        { ...current, status: 'current' },
        { ...next, status: 'future' },
      ],
      date: '2026-08-09',
      available_dates: ['2026-08-09'],
    })
  }

  const { wrapper, store } = await mountPlayer()
  store.currentIptvChannel = channels[1]
  await flushPromises()
  await wrapper.vm.$nextTick()

  assert.equal(domElement('.mobile-now-playing__current').textContent.trim(), current.title)
  assert.match(domElement('.mobile-now-playing__next').textContent, /下一节目 · 下一档新闻 \d{2}:\d{2}/)
  assert.equal(domElements('.desktop-video-overlay .progress-times').length, 1)
  assert.equal(domElements('.mobile-video-overlay .progress-times').length, 1)
  assert.equal(domElements('.desktop-video-overlay [role="progressbar"]').length, 1)
  assert.equal(domElements('.desktop-video-overlay .progress-knob').length, 0)
  assert.notEqual(domElement('.progress-fill').style.width, '0%')
  assert.match(domElement('.mobile-now-playing__remaining').textContent, /剩余 \d+ 分钟/)
  assert.match(domElement('.mobile-video-overlay-status').textContent, /直播中|正在连接|已暂停/)
  assert.equal(store.currentEpgProgram?.title, current.title)
})

test('FullPlayer 无 next 时不占位', async () => {
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return response({
      current: { title: '只有当前节目', start: '2026-08-09T10:00:00+08:00', stop: '2026-08-09T11:00:00+08:00' },
      next: null,
      programs: [],
      date: '2026-08-09',
      available_dates: [],
    })
  }
  const { wrapper, store } = await mountPlayer()
  store.currentIptvChannel = channels[1]
  await flushPromises()
  await wrapper.vm.$nextTick()
  assert.equal(domElement('.mobile-now-playing__current').textContent.trim(), '只有当前节目')
  assert.equal(domElements('.mobile-now-playing__next').length, 0)
})

test('IPTV Mobile 使用 compact Now Playing 和统一 sticky panel header', async () => {
  const { store } = await mountPlayer()
  await flushPromises()

  const root = domElement('.full-player')
  assert.equal(root.classList.contains('full-player--mobile-iptv'), true)
  assert.equal(domElements('.mobile-now-playing').length, 1)
  assert.equal(domElements('.now-metadata').length, 0)
  assert.equal(domElements('.mobile-now-playing__logo').length, 1)
  assert.equal(domElements('.mobile-now-playing button').length, 0)
  assert.equal(domElements('.mobile-panel-header').length, 1)
  assert.equal(domElements('.mobile-panel-header .panel-tabs button').length, 2)
  assert.equal(domElements('.mobile-panel-header .sort-btn').length, 1)
  assert.equal(domElements('.mobile-panel .channel-sort-bar').length, 0)
  assert.match(domElement('.mobile-panel-header .sort-btn').textContent, /默认排序/)
  assert.equal(domElement('.mobile-panel-header .panel-tabs button').getAttribute('type'), 'button')
  assert.equal(domElement('.mobile-panel-header .sort-btn').getAttribute('type'), 'button')
  assert.equal(store.currentIptvChannel.name, 'Alpha')
})

test('IPTV Mobile 无 EPG 时 schedule shell 不保留空白，Radio 保留既有 metadata shell', async () => {
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return response({ current: null, next: null, programs: [], date: '2026-08-09', available_dates: [] })
  }
  await mountPlayer()
  await clickDom('.mobile-panel-header .panel-tabs button', 1)
  await flushPromises()
  assert.equal(domElements('.schedule-panel.schedule-panel--empty').length, 1)
  assert.equal(domElements('.schedule-panel--empty .schedule-content').length, 0)

  for (const wrapper of mountedWrappers.splice(0)) {
    if (wrapper.exists()) wrapper.unmount()
  }
  document.body.innerHTML = '<div id="app"></div>'
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.activeMode = 'radio'
  store.currentStation = 'radio-a'
  store.stationMap = { 'radio-a': { id: 'radio-a', name: '测试电台', subtitle: '音乐' } }
  store.isPlayerExpanded = true
  const wrapper = mount(FullPlayer, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.vm.$nextTick()
  assert.equal(domElements('.full-player--mobile-iptv').length, 0)
  assert.equal(domElements('.mobile-now-playing').length, 0)
  assert.equal(domElements('.now-metadata').length, 1)
  assert.equal(domElements('.mobile-panel-header').length, 0)
})

test('FullPlayer 非 IPTV 节目单继续保留电台名 fallback', async () => {
  setActivePinia(createPinia())
  const store = usePlayerStore()
  store.activeMode = 'radio'
  store.currentStation = 'radio-a'
  store.stationMap = {
    'radio-a': { id: 'radio-a', name: '测试电台', subtitle: '音乐' },
  }
  store.isPlayerExpanded = true
  const wrapper = mount(FullPlayer, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.vm.$nextTick()

  assert.equal(domElements('.now-program-title').length, 0)
  await clickDom('.mobile-panel .panel-tabs button', 1)
  assert.ok(domElements('.timeline-title').some((item) => item.textContent.trim() === '测试电台'))
})

test('FullPlayer 无 EPG 时隐藏节目元数据且不阻塞直播', async () => {
  let pendingResolve
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return await new Promise((resolve) => { pendingResolve = resolve })
  }
  const first = await mountPlayer()
  first.store.currentIptvChannel = channels[1]
  await flushPromises()
  assert.equal(domElements('.now-programme').length, 0)
  assert.equal(first.store.isPlaying, true)
  pendingResolve(response({ current: null, next: null, programs: [], date: '2026-08-09', available_dates: [] }))
  await flushPromises()
  assert.equal(domElements('.now-programme').length, 0)
  assert.doesNotMatch(document.body.textContent, /暂无节目单/)

  first.wrapper.unmount()
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return response({}, { ok: false, status: 503 })
  }
  const second = await mountPlayer()
  second.store.currentIptvChannel = channels[1]
  await flushPromises()
  assert.equal(domElements('.now-programme').length, 0)
  assert.equal(second.store.isPlaying, true)
  assert.equal(second.store.playbackError, '')
})

test('IptvHome batch-current 只显示 current 节目名并在最近节目边界后自动更新', async () => {
  channels = [channel('Alpha', 'alpha', { group_name: '测试分组' })]
  let batchCount = 0
  const bodies = []
  fetchOverride = async (url, options) => {
    if (!url.includes('/api/iptv/epg/batch-current')) return null
    bodies.push(JSON.parse(options.body))
    batchCount += 1
    const stop = new Date(Date.now() + (batchCount === 1 ? 80 : 80)).toISOString()
    return response({
      alpha: {
        current: { title: batchCount === 1 ? '第一档节目' : '第二档节目', stop },
        next: null,
      },
    })
  }

  const { wrapper } = await mountHome()
  assert.equal(domElement('.card-program-name').textContent.trim(), '第一档节目')
  assert.deepEqual(bodies[0], { canonical_keys: ['alpha'] })
  await new Promise((resolve) => setTimeout(resolve, 1_350))
  await flushPromises()
  assert.equal(batchCount, 2)
  assert.equal(domElement('.card-program-name').textContent.trim(), '第二档节目')

  wrapper.unmount()
  await new Promise((resolve) => setTimeout(resolve, 1_350))
  assert.equal(batchCount, 2)
})

test('IptvHome 搜索切换会清理旧列表 EPG timer，且无 EPG 回退分组', async () => {
  channels = [
    channel('Alpha', 'alpha', { group_name: '旧分组' }),
    channel('Bravo', 'bravo', { group_name: '新分组' }),
  ]
  const batchKeys = []
  fetchOverride = async (url, options) => {
    if (!url.includes('/api/iptv/epg/batch-current')) return null
    const keys = JSON.parse(options.body).canonical_keys
    batchKeys.push(keys)
    if (keys.includes('alpha')) {
      return response({
        alpha: { current: { title: '即将结束的旧节目', stop: new Date(Date.now() + 80).toISOString() }, next: null },
      })
    }
    return response({ bravo: null })
  }

  const { wrapper, searchQuery } = await mountHome({ search: 'Alpha' })
  assert.match(domElement('.card-program-name').textContent, /即将结束的旧节目/)
  searchQuery.value = 'Bravo'
  await flushPromises()
  await wrapper.vm.$nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
  assert.equal(domElement('.card-program-name').textContent.trim(), '新分组')
  await new Promise((resolve) => setTimeout(resolve, 1_350))
  await flushPromises()
  assert.equal(batchKeys.filter((keys) => keys.includes('alpha')).length, 1)
  assert.equal(batchKeys.filter((keys) => keys.includes('bravo')).length, 1)
})

test('EPG 日期切换与频道切换后，节目单属于当前频道且旧请求不覆盖新请求', async () => {
  const { wrapper, store } = await mountPlayer()
  await clickDom('.side-panel .panel-tabs button', 1)
  if (domElements('.side-panel .schedule-date-chip').length) {
    await clickDom('.side-panel .schedule-date-chip', 0)
  }
  await clickDom('.side-panel .panel-tabs button', 0)
  await clickDom('.side-panel .channel-row', 1)
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Bravo')
  assert.match(document.body.textContent, /Bravo|2026-08-05-current/)
  assert.equal(activeMediaCount(), 1)
})

test('EPG 日期和频道请求交错时，慢请求不能覆盖当前频道和日期', async () => {
  const pending = []
  fetchOverride = async (url) => {
    if (!url.includes('/api/iptv/epg/programs/')) return null
    return await new Promise((resolve, reject) => pending.push({ url, resolve, reject }))
  }
  const { wrapper, store } = await mountPlayer()
  await flushPromises()
  const rows = domElements('.side-panel .channel-row')
  rows[1].click()
  await flushPromises()
  rows[2].click()
  await flushPromises()
  assert.ok(pending.length >= 2)
  const bravoRequest = pending[0]
  const charlieRequest = pending[1]
  charlieRequest.resolve(response({
    current: { title: 'Charlie-base', start: '2026-08-05T10:00:00Z', stop: '2026-08-05T11:00:00Z' },
    next: null,
    programs: [],
    date: '2026-08-05',
    available_dates: ['2026-08-04', '2026-08-05', '2026-08-06'],
  }))
  await flushPromises()
  await new Promise(resolve => setTimeout(resolve, 0))
  await flushPromises()
  assert.equal(store.currentEpgProgram?.title, 'Charlie-base')
  await clickDom('.side-panel .panel-tabs button', 1)
  const dates = domElements('.side-panel .schedule-date-chip')
  assert.equal(dates.length, 3)
  dates[0].click()
  dates[2].click()
  await flushPromises()
  assert.ok(pending.length >= 4)
  const newest = pending.at(-1)
  newest.resolve(response({
    current: { title: 'Charlie-new', start: '2026-08-05T10:00:00Z', stop: '2026-08-05T11:00:00Z' },
    next: null,
    programs: [],
    date: '2026-08-06',
    available_dates: ['2026-08-05', '2026-08-06'],
  }))
  await flushPromises()
  pending[2].resolve(response({
    current: { title: 'Charlie-old-date', start: '2026-08-04T10:00:00Z', stop: '2026-08-04T11:00:00Z' },
    next: null,
    programs: [],
    date: '2026-08-04',
    available_dates: ['2026-08-04'],
  }))
  bravoRequest.resolve(response({
    current: { title: 'Bravo-old', start: '2026-08-04T10:00:00Z', stop: '2026-08-04T11:00:00Z' },
    next: null,
    programs: [],
    date: '2026-08-04',
    available_dates: ['2026-08-04'],
  }))
  await flushPromises()
  assert.equal(store.currentIptvChannel.name, 'Charlie')
  assert.match(document.body.textContent, /Charlie-new/)
})

test('auth 停播后卸载资源，重新进入普通页面不会自动起播', async () => {
  const { wrapper, store } = await mountPlayer()
  await settlePlayback()
  assert.equal(activeMediaCount(), 1)
  store.stopAndClearPlayback()
  await wrapper.vm.$nextTick()
  wrapper.unmount()
  assert.equal(store.currentIptvChannel, null)
  assert.equal(store.iptvChannelContext, null)
  assert.equal(store.iptvVideoEl, null)
  assert.equal(store.isPlaying, false)
  assert.equal(document.querySelectorAll('video, iframe').length, 0)

  const next = mount(FullPlayer, { attachTo: document.body })
  mountedWrappers.push(next)
  await flushPromises()
  assert.equal(activeMediaCount(), 0)
  assert.equal(store.currentIptvChannel, null)
})

test('收起再展开保持当前频道/source；同一频道重复点击不制造第二个媒体元素', async () => {
  const { wrapper, store } = await mountPlayer()
  const sourceToken = store.iptvSelectionToken
  await clickDom('.desktop-collapse-btn')
  assert.equal(store.isPlayerExpanded, false)
  store.expandPlayer()
  await wrapper.vm.$nextTick()
  await clickDom('.side-panel .channel-row', 0)
  assert.equal(store.currentIptvChannel.name, 'Alpha')
  assert.equal(store.iptvSelectionToken, sourceToken + 1)
  assert.equal(activeMediaCount(), 1)
})
