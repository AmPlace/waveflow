import assert from 'node:assert/strict'
import { before, after, afterEach, test } from 'node:test'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { JSDOM } from 'jsdom'

let dom, dir, mount, flushPromises, Component, wrapper
const globals = new Map()
const state = () => globalThis.__marketAutomationTest
const fixture = () => ({ enabled: true, interval_seconds: 86400, minimum_interval_seconds: 300, maximum_interval_seconds: 2678400, service_started: true })
before(async () => {
  dom = new JSDOM('<!doctype html><html><body></body></html>')
  for (const key of ['window', 'document', 'Document', 'navigator', 'Element', 'Node', 'SVGElement', 'HTMLElement']) {
    globals.set(key, Object.getOwnPropertyDescriptor(globalThis, key))
    Object.defineProperty(globalThis, key, { configurable: true, value: key === 'window' ? dom.window : dom.window[key] })
  }
  ;({ mount, flushPromises } = await import('@vue/test-utils'))
  const { build } = await import('vite')
  const { default: vue } = await import('@vitejs/plugin-vue')
  dir = await mkdtemp(path.join(tmpdir(), 'waveflow-market-automation-'))
  await build({ configFile: false, logLevel: 'silent', plugins: [{
    name: 'automation-test-api', enforce: 'pre',
    resolveId(id) { if (id === '../api/marketAutomation.js') return '\0automation-api' },
    load(id) { if (id === '\0automation-api') return 'export const fetchMarketAutomation=(...a)=>globalThis.__marketAutomationTest.load(...a); export const updateMarketAutomation=(...a)=>globalThis.__marketAutomationTest.save(...a)' },
  }, vue()], build: { outDir: dir, lib: { entry: new URL('../../src/components/MarketAutomationSettings.vue', import.meta.url).pathname, formats: ['es'], fileName: () => 'view.mjs' }, rollupOptions: { external: ['vue'], output: { paths: { vue: new URL('../../node_modules/vue/index.js', import.meta.url).href } } } } })
  Component = (await import(pathToFileURL(path.join(dir, 'view.mjs')))).default
})
async function open(data = fixture(), overrides = {}) {
  globalThis.__marketAutomationTest = { data, calls: [], load: async () => structuredClone(data), save: async payload => { state().calls.push(payload); Object.assign(data, payload); return structuredClone(data) }, ...overrides }
  wrapper = mount(Component, { attachTo: document.body }); await flushPromises()
}
async function submit() { await wrapper.get('form').trigger('submit'); await flushPromises() }
afterEach(() => { wrapper?.unmount(); wrapper = null; delete globalThis.__marketAutomationTest })
after(async () => {
  dom?.window.close()
  for (const [key, descriptor] of globals) descriptor ? Object.defineProperty(globalThis, key, descriptor) : delete globalThis[key]
  if (dir) await rm(dir, { recursive: true, force: true })
})
test('backend value loads without writes; interval conversion and remount use saved authority', async () => {
  await open()
  assert.equal(wrapper.get('input[type=number]').element.value, '1440')
  assert.equal(state().calls.length, 0)
  await wrapper.get('input[type=number]').setValue('60'); await submit()
  assert.deepEqual(state().calls, [{ interval_seconds: 3600 }])
  const saved = structuredClone(state().data); wrapper.unmount(); await open(saved)
  assert.equal(wrapper.get('input[type=number]').element.value, '60')
})
test('invalid blank, below/above bounds and fractional seconds do not write', async () => {
  await open()
  for (const value of ['', '0', '4', '44641', '5.001']) {
    await wrapper.get('input[type=number]').setValue(value); await submit()
    assert.equal(state().calls.length, 0)
    assert.equal(wrapper.get('input[type=number]').attributes('aria-invalid'), 'true')
  }
})
test('disabling preserves stored interval and enable does not run refresh', async () => {
  await open()
  await wrapper.get('input[type=number]').setValue('60')
  await wrapper.get('[role=switch]').setValue(false)
  assert.equal(wrapper.get('input[type=number]').element.disabled, true)
  await submit()
  assert.deepEqual(state().calls, [{ enabled: false }])
  assert.equal(wrapper.get('input[type=number]').element.value, '1440')
  await wrapper.get('[role=switch]').setValue(true); await submit()
  assert.deepEqual(state().calls[1], { enabled: true })
})
test('load unavailable can retry; stopped service is not shown as scheduled', async () => {
  await open(fixture(), { load: async () => { throw { status: 503 } } })
  assert.equal(wrapper.find('form').exists(), false)
  state().load = async () => ({ ...fixture(), service_started: false })
  await wrapper.get('button').trigger('click'); await flushPromises()
  assert.match(wrapper.text(), /调度服务未运行/)
})
test('save failure retains draft and permits retry; pending request freezes input', async () => {
  await open(); const save = state().save
  let reject
  state().save = () => new Promise((resolve, r) => { reject = r })
  await wrapper.get('input[type=number]').setValue('60'); await submit()
  assert.equal(wrapper.get('[role=switch]').element.disabled, true)
  reject({ status: 503 }); await flushPromises()
  assert.equal(wrapper.get('input[type=number]').element.value, '60')
  assert.match(wrapper.text(), /服务暂时不可用/)
  state().save = save; await submit()
  assert.equal(state().data.interval_seconds, 3600)
})
test('closing aborts pending load; mount does not schedule a timer or task', async () => {
  let signal, resolve
  await open(fixture(), { load: options => { signal = options.signal; return new Promise(r => { resolve = r }) } })
  wrapper.unmount(); wrapper = null
  assert.equal(signal.aborted, true)
  resolve(fixture()); await flushPromises()
  assert.equal(state().calls.length, 0)
})
test('API uses existing GET/PATCH contract, and panel is inside source dialog', async () => {
  const api = await readFile(new URL('../../src/api/marketAutomation.js', import.meta.url), 'utf8')
  assert.match(api, /method: 'PATCH'/)
  assert.match(api, /\/api\/admin\/market\/automation/)
  assert.doesNotMatch(api, /updates\/run|refresh'|setInterval/)
  const view = await readFile(new URL('../../src/views/MarketView.vue', import.meta.url), 'utf8')
  assert.ok(view.indexOf('<MarketAutomationSettings />') > view.indexOf('v-if="sourceDialogOpen"'))
})
