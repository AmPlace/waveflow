import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { parse, compileStyle } from '@vue/compiler-sfc'
import { SECURITY_FIELDS, HIDDEN_RUNTIME_FIELDS, settingsDraft, settingsPatch, settingsRisks, validateSettingsPatch } from '../../src/views/settings/securitySettingsUi.js'

const schema = Object.fromEntries(SECURITY_FIELDS.map(item => [item.key, { type: item.type, min: 1, max: 32 }]))
test('only eight consumer-backed controls are exposed, not every persisted key', () => {
  assert.equal(SECURITY_FIELDS.length, 8)
  for (const key of HIDDEN_RUNTIME_FIELDS) assert.equal(SECURITY_FIELDS.some(item => item.key === key), false)
})
test('locked controls show effective values without overwriting stored authority', () => {
  const data = { settings: { anonymous_browse: false }, runtime: { anonymous_browse: true }, forced: ['anonymous_browse'] }
  assert.equal(settingsDraft(data).anonymous_browse, false)
  assert.equal(data.runtime.anonymous_browse, true)
  assert.deepEqual(settingsPatch({ anonymous_browse: true }, { anonymous_browse: false }, new Set(data.forced), schema), {})
})
test('save payload is changed-only, typed, capability-bound and excludes disabled dependencies', () => {
  assert.deepEqual(settingsPatch({ session_max_age_days: 15, probe_concurrency: 2, rtsp_max_sessions: 4, enable_rtsp_proxy: false }, { session_max_age_days: 14, enable_rtsp_proxy: false }, new Set(), schema), { session_max_age_days: 15 })
  assert.deepEqual(settingsPatch({ session_max_age_days: 15 }, {}, new Set(), {}), {})
  assert.deepEqual(settingsPatch({ enable_rtsp_proxy: true, rtsp_max_sessions: 4 }, { enable_rtsp_proxy: false, rtsp_max_sessions: 8 }, new Set(), schema), { enable_rtsp_proxy: true, rtsp_max_sessions: 4 })
})
test('blank, coerced, fractional and out-of-range numbers fail validation', () => {
  for (const value of ['', '3', NaN, 1.5, 0, 33, true]) assert.ok(validateSettingsPatch({ session_max_age_days: value }, schema).session_max_age_days)
  assert.deepEqual(validateSettingsPatch({ session_max_age_days: 14 }, schema), {})
})
test('both widening and restricting access need confirmation; numeric changes do not', () => {
  assert.equal(settingsRisks({ anonymous_browse: true }).length, 1)
  assert.equal(settingsRisks({ allow_private: false }).length, 1)
  assert.deepEqual(settingsRisks({ session_max_age_days: 15 }), [])
})

test('scoped Dark overrides compile to specific targets, never bare html theme rules', () => {
  for (const file of ['SecuritySettings.vue', 'PluginsSettings.vue']) {
    const { descriptor } = parse(readFileSync(new URL('../../src/views/settings/' + file, import.meta.url), 'utf8'))
    const result = compileStyle({ source: descriptor.styles[0].content, id: 'data-v-test', scoped: true })
    assert.deepEqual(result.errors, [])
    assert.doesNotMatch(result.code, /(?:^|\n)\s*(?:html)?\.dark\s*\{/)
    assert.match(result.code, /html\.dark \.(?:security|plugin)-/)
  }
})

test('developer install stays disclosed and server-path based, with enabling confirmation', () => {
  const source = readFileSync(new URL('../../src/views/settings/PluginsSettings.vue', import.meta.url), 'utf8')
  assert.match(source, /<details class="plugin-developer/)
  assert.match(source, /服务器上的 package/)
  assert.doesNotMatch(source, /type="file"|file\?\.name/)
  assert.match(source, /!developerMode\.value && !await toastStore\.askConfirm/)
  assert.match(source, /!developerLoaded/)
})
