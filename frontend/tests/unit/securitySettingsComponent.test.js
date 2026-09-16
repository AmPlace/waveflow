import assert from 'node:assert/strict'
import { after, afterEach, before, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { JSDOM } from 'jsdom'
import { SECURITY_FIELDS } from '../../src/views/settings/securitySettingsUi.js'

let dom, dir, mount, flushPromises, Component, wrapper
const originalGlobals = new Map()
const h = () => globalThis.__settingsTest
function fixture() {
  const settings = Object.fromEntries(SECURITY_FIELDS.map(item => [item.key, item.type === 'bool' ? false : 8]))
  return { mode: 'nas', settings, runtime: { ...settings }, defaults: { ...settings }, forced: [], schema: Object.fromEntries(SECURITY_FIELDS.map(item => [item.key, { type: item.type, min: 1, max: 365 }])) }
}
before(async () => {
  dom = new JSDOM('<!doctype html><html><body></body></html>')
  for (const key of ['window', 'document', 'Document', 'navigator', 'Element', 'Node', 'SVGElement', 'HTMLElement']) {
    originalGlobals.set(key, Object.getOwnPropertyDescriptor(globalThis, key))
    Object.defineProperty(globalThis, key, { configurable: true, value: key === 'window' ? dom.window : dom.window[key] })
  }
  ;({ mount, flushPromises } = await import('@vue/test-utils'))
  const { build } = await import('vite')
  const { default: vue } = await import('@vitejs/plugin-vue')
  dir = await mkdtemp(path.join(tmpdir(), 'waveflow-settings-component-'))
  await build({ configFile: false, logLevel: 'silent', plugins: [{
    name: 'settings-test-api', enforce: 'pre',
    resolveId(id) {
      if (id === '../../api/settings') return '\0settings-api'
      if (id === '../../stores/auth') return '\0settings-auth'
      if (id === '../../stores/toast') return '\0settings-toast'
    },
    load(id) {
      if (id === '\0settings-api') return 'export const fetchSecuritySettings = (...a) => globalThis.__settingsTest.load(...a); export const updateSecuritySettings = (...a) => globalThis.__settingsTest.save(...a)'
      if (id === '\0settings-auth') return 'export const useAuthStore = () => ({setup:{}})'
      if (id === '\0settings-toast') return 'export const useToastStore = () => ({askConfirm: (...a) => globalThis.__settingsTest.confirm(...a), success: () => {}})'
    },
  }, vue()], build: { outDir: dir, lib: { entry: new URL('../../src/views/settings/SecuritySettings.vue', import.meta.url).pathname, formats: ['es'], fileName: () => 'view.mjs' }, rollupOptions: { external: ['vue'], output: { paths: { vue: new URL('../../node_modules/vue/index.js', import.meta.url).href } } } } })
  Component = (await import(pathToFileURL(path.join(dir, 'view.mjs')))).default
})
async function open(data = fixture()) {
  globalThis.__settingsTest = { data, calls: [], load: async () => structuredClone(data), confirm: async () => false, save: async payload => { h().calls.push(payload); Object.assign(data.settings, payload); Object.assign(data.runtime, payload); return structuredClone(data) } }
  wrapper = mount(Component, { attachTo: document.body })
  await flushPromises()
  return wrapper
}
afterEach(() => { wrapper?.unmount(); wrapper = null; delete globalThis.__settingsTest })
after(async () => {
  dom?.window.close()
  for (const [key, value] of originalGlobals) value ? Object.defineProperty(globalThis, key, value) : delete globalThis[key]
  if (dir) await rm(dir, { recursive: true, force: true })
})
test('forced effective state is visible and disabled; unsupported fields are absent', async () => {
  const data = fixture(); data.settings.anonymous_browse = false; data.runtime.anonymous_browse = true; data.forced = ['anonymous_browse']
  await open(data)
  assert.equal(wrapper.get('#anonymous_browse').element.checked, false)
  assert.equal(wrapper.get('#anonymous_browse').element.disabled, true)
  assert.equal(wrapper.find('#public_base_url').exists(), false)
  assert.equal(wrapper.get('#rtsp_max_sessions').element.disabled, true)
})
test('invalid input never calls API and successful save persists across remount', async () => {
  await open()
  await wrapper.get('#session_max_age_days').setValue('0')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.equal(h().calls.length, 0)
  assert.equal(wrapper.get('#session_max_age_days').attributes('aria-invalid'), 'true')
  await wrapper.get('#session_max_age_days').setValue('15')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.deepEqual(h().calls, [{ session_max_age_days: 15 }])
  const saved = structuredClone(h().data); wrapper.unmount(); await open(saved)
  assert.equal(wrapper.get('#session_max_age_days').element.value, '15')
})
test('risk cancel does not write and confirmation freezes input until resolved', async () => {
  await open()
  let resolve
  h().confirm = () => new Promise(r => { resolve = r })
  await wrapper.get('#anonymous_playback').setValue(true)
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.equal(wrapper.get('#anonymous_playback').element.disabled, true)
  assert.equal(h().calls.length, 0)
  resolve(false); await flushPromises()
  assert.equal(h().calls.length, 0)
  assert.equal(wrapper.get('#anonymous_playback').element.disabled, false)
  h().confirm = async () => true
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.deepEqual(h().calls, [{ anonymous_playback: true }])
})
test('save error retains draft, retry works; discarded values never save', async () => {
  await open(); const save = h().save
  h().save = async () => { throw { status: 503 } }
  await wrapper.get('#session_max_age_days').setValue('15')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.equal(wrapper.get('#session_max_age_days').element.value, '15')
  assert.match(wrapper.text(), /服务暂时不可用/)
  h().save = save
  await wrapper.get('form').trigger('submit'); await flushPromises()
  assert.equal(h().data.runtime.session_max_age_days, 15)
  await wrapper.get('#session_max_age_days').setValue('16')
  await wrapper.findAll('button').find(x => x.text() === '撤销更改').trigger('click')
  assert.equal(wrapper.get('#session_max_age_days').element.value, '15')
})
test('unmount during confirmation cannot write', async () => {
  await open(); let resolve
  h().confirm = () => new Promise(r => { resolve = r })
  await wrapper.get('#anonymous_playback').setValue(true)
  await wrapper.get('form').trigger('submit'); await flushPromises()
  wrapper.unmount(); wrapper = null
  resolve(true); await flushPromises()
  assert.equal(h().calls.length, 0)
})
