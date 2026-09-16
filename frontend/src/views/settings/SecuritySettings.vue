<template>
  <section class="security-settings" aria-labelledby="security-settings-title">
    <header class="security-header">
      <div>
        <h2 id="security-settings-title">安全与访问</h2>
        <p>管理访客权限、登录有效期和媒体网络访问</p>
      </div>
      <div v-if="loaded" class="security-actions">
        <span v-if="dirty" class="security-dirty" role="status">有未保存的更改</span>
        <button type="button" class="security-button" :disabled="saving || !dirty" @click="discard">撤销更改</button>
        <button type="submit" form="security-form" class="security-button security-save" :disabled="saving || !dirty">
          {{ saving ? '保存中…' : '保存设置' }}
        </button>
      </div>
    </header>

    <div v-if="loading" class="security-loading" aria-label="正在加载安全设置" aria-busy="true"></div>
    <div v-else-if="error && !loaded" class="security-feedback is-error" role="alert">
      <h3>安全设置加载失败</h3>
      <p>{{ error }}</p>
      <button type="button" class="security-button" @click="load">重试</button>
    </div>

    <form v-else id="security-form" novalidate @submit.prevent="save">
      <p class="security-mode">当前部署：{{ modeLabel }} · 环境变量优先于已保存值和部署默认值</p>
      <p v-if="error" class="security-feedback is-error" role="alert">{{ error }}</p>
      <p v-if="success" class="security-feedback" role="status">{{ success }}</p>

      <template v-for="group in SECURITY_GROUPS" :key="group.key">
        <component :is="group.advanced ? 'details' : 'section'" class="security-section">
          <component :is="group.advanced ? 'summary' : 'div'" class="security-section-heading">
            <h3>{{ group.advanced ? '高级 · ' : '' }}{{ group.title }}</h3>
            <p>{{ group.description }}</p>
          </component>
          <div class="security-fields">
            <div v-for="item in SECURITY_FIELDS.filter(field => field.group === group.key)" :key="item.key" class="security-field">
              <div class="security-field-copy">
                <label :for="item.key">{{ item.label }}</label>
                <p :id="item.key + '-help'">{{ item.description }}</p>
                <span v-if="isForced(item.key)" class="security-field-note">环境变量锁定 · 当前生效：{{ formatValue(effective[item.key]) }}</span>
                <span v-else-if="!fieldAvailable(item, schema)" class="security-field-note">当前服务器未提供此设置</span>
                <span v-else-if="item.dependsOn && draft[item.dependsOn] !== true" class="security-field-note">启用 RTSP 转换后可调整，已保存的上限保留</span>
                <span v-if="schema[item.key]?.restart_required" class="security-field-note">重启后生效</span>
                <p v-if="fieldErrors[item.key]" :id="item.key + '-error'" class="security-field-error" role="alert">{{ fieldErrors[item.key] }}</p>
              </div>
              <label v-if="item.type === 'bool'" class="security-switch">
                <input :id="item.key" v-model="draft[item.key]" type="checkbox" role="switch" :aria-label="item.label"
                  :aria-describedby="item.key + '-help'" :disabled="fieldDisabled(item)" />
                <span aria-hidden="true"></span>
              </label>
              <div v-else class="security-number">
                <input :id="item.key" v-model.number="draft[item.key]" type="number" step="1" required
                  :min="schema[item.key]?.min" :max="schema[item.key]?.max" :aria-label="item.label"
                  :aria-invalid="Boolean(fieldErrors[item.key])"
                  :aria-describedby="item.key + '-help' + (fieldErrors[item.key] ? ' ' + item.key + '-error' : '')"
                  :disabled="fieldDisabled(item)" />
                <span>{{ item.unit }}</span>
              </div>
            </div>
          </div>
        </component>
      </template>

      <details class="security-section security-effective">
        <summary class="security-section-heading"><h3>生效值与保存来源</h3></summary>
        <dl>
          <div v-for="item in SECURITY_FIELDS" :key="item.key">
            <dt>{{ item.label }}</dt>
            <dd>当前生效：{{ formatValue(effective[item.key]) }} · 已保存：{{ storedValue(item.key) }}</dd>
          </div>
        </dl>
      </details>
    </form>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchSecuritySettings, updateSecuritySettings } from '../../api/settings'
import { adminRequestErrorMessage } from '../../api/adminUi.js'
import { useAuthStore } from '../../stores/auth'
import { useToastStore } from '../../stores/toast'
import { SECURITY_FIELDS, SECURITY_GROUPS, fieldAvailable, settingsDraft, settingsPatch, settingsRisks, validateSettingsPatch } from './securitySettingsUi.js'

const authStore = useAuthStore()
const toastStore = useToastStore()
const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const error = ref('')
const success = ref('')
const fieldErrors = ref({})
const forced = ref(new Set())
const effective = ref({})
const persisted = ref({})
const defaults = ref({})
const schema = ref({})
const mode = ref('')
const draft = ref({})
const baselineDraft = ref({})
let loadController = null
let saveController = null
let saveRequestId = 0
let componentDisposed = false
const dirty = computed(() => SECURITY_FIELDS.some(item => !isForced(item.key)
  && fieldAvailable(item, schema.value) && draft.value[item.key] !== baselineDraft.value[item.key]))
const modeLabel = computed(() => ({ desktop: '桌面版', nas: '家庭服务器 / NAS', public: '公共服务' })[mode.value] || '未知')

function apply(data) {
  const settings = data?.settings || {}
  effective.value = { ...settings }
  persisted.value = { ...(data?.runtime || {}) }
  defaults.value = { ...(data?.defaults || {}) }
  schema.value = { ...(data?.schema || {}) }
  mode.value = String(data?.mode || '')
  forced.value = new Set(data?.forced || [])
  draft.value = settingsDraft(data)
  baselineDraft.value = { ...draft.value }
  if (settings.anonymous_browse !== undefined) authStore.setup.anonymousBrowse = Boolean(settings.anonymous_browse)
  if (settings.anonymous_playback !== undefined) authStore.setup.anonymousPlayback = Boolean(settings.anonymous_playback)
  loaded.value = true
}
function isForced(key) { return forced.value.has(key) }
function fieldDisabled(item) {
  return saving.value || isForced(item.key) || !fieldAvailable(item, schema.value)
    || Boolean(item.dependsOn && draft.value[item.dependsOn] !== true)
}
function formatValue(value) {
  if (value === true) return '开启'
  if (value === false) return '关闭'
  if (value === '' || value === undefined || value === null) return '未设置'
  return String(value)
}
function storedValue(key) {
  return Object.prototype.hasOwnProperty.call(persisted.value, key)
    ? formatValue(persisted.value[key])
    : `未单独保存（默认 ${formatValue(defaults.value[key])}）`
}
function discard() {
  draft.value = { ...baselineDraft.value }
  fieldErrors.value = {}
  error.value = ''
  success.value = ''
}
async function load() {
  loadController?.abort()
  const controller = new AbortController()
  loadController = controller
  loading.value = true
  error.value = ''
  try {
    const data = await fetchSecuritySettings({ signal: controller.signal })
    if (!controller.signal.aborted && !componentDisposed) apply(data)
  } catch (caught) {
    if (!controller.signal.aborted && !componentDisposed) error.value = adminRequestErrorMessage(caught, '安全设置加载失败')
  } finally {
    if (loadController === controller && !componentDisposed) loading.value = false
  }
}
async function save() {
  if (!loaded.value || saving.value || !dirty.value || componentDisposed) return
  const payload = settingsPatch(draft.value, baselineDraft.value, forced.value, schema.value)
  fieldErrors.value = validateSettingsPatch(payload, schema.value)
  error.value = ''
  success.value = ''
  if (Object.keys(fieldErrors.value).length) {
    error.value = '请检查标出的设置项'
    return
  }
  if (!Object.keys(payload).length) {
    error.value = '没有可保存的更改，请检查已停用设置的依赖或撤销更改'
    return
  }
  const requestId = ++saveRequestId
  saving.value = true
  try {
    const risks = settingsRisks(payload)
    if (risks.length && !await toastStore.askConfirm({
      title: '确认修改访问与播放策略',
      message: risks.join('\n'),
      confirmText: '确认并保存',
      danger: true,
    })) return
    if (requestId !== saveRequestId || componentDisposed) return
    saveController?.abort()
    const controller = new AbortController()
    saveController = controller
    const data = await updateSecuritySettings(payload, { signal: controller.signal })
    if (requestId !== saveRequestId || controller.signal.aborted || componentDisposed) return
    apply(data)
    success.value = Object.keys(payload).some(key => schema.value[key]?.restart_required)
      ? '安全设置已保存，部分设置将在后端重启后生效'
      : '安全设置已保存'
    toastStore.success(success.value)
  } catch (caught) {
    if (requestId === saveRequestId && !saveController?.signal.aborted && !componentDisposed) {
      error.value = adminRequestErrorMessage(caught, '安全设置保存失败')
    }
  } finally {
    if (requestId === saveRequestId && !componentDisposed) saving.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  componentDisposed = true
  saveRequestId += 1
  loadController?.abort()
  saveController?.abort()
})
</script>

<style scoped>
.security-settings { min-width: 0; color: var(--text-primary); }
.security-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: var(--card-gap); }
.security-header h2 { font-size: 20px; font-weight: 600; line-height: 28px; }
.security-header p, .security-section-heading p { margin-top: 6px; color: var(--text-secondary); font-size: 13px; line-height: 20px; }
.security-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.security-button { min-height: var(--control-height); padding: 0 14px; border: 1px solid var(--border); border-radius: var(--control-radius); background: var(--surface); color: var(--text-primary); font-size: 13px; font-weight: 500; }
.security-button:hover:not(:disabled) { background: var(--surface-hover); border-color: var(--border-strong); }
.security-save { background: var(--text-primary); border-color: var(--text-primary); color: var(--bg); }
.security-save:hover:not(:disabled) { background: var(--text-primary); opacity: .9; }
.security-button:disabled { opacity: .45; cursor: not-allowed; }
.security-button:focus-visible, summary:focus-visible { outline: 2px solid var(--text-secondary); outline-offset: 3px; }
.security-dirty, .security-mode { font-size: 12px; color: var(--text-secondary); line-height: 20px; }
.security-mode { margin-bottom: 12px; }
.security-section { padding-block: 18px; border-top: 1px solid var(--border); }
.security-section-heading h3 { display: inline; font-size: 14px; font-weight: 600; line-height: 20px; }
summary.security-section-heading { cursor: pointer; }
.security-fields { margin-top: 8px; }
.security-field { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding-block: 14px; }
.security-field + .security-field { border-top: 1px solid var(--border); }
.security-field-copy { min-width: 0; }
.security-field-copy label { font-size: 14px; font-weight: 500; color: var(--text-primary); }
.security-field-copy p { font-size: 12px; color: var(--text-secondary); line-height: 20px; margin-top: 4px; }
.security-field-note { display: block; color: var(--text-tertiary); font-size: 12px; margin-top: 4px; }
.security-number { display: flex; align-items: center; gap: 8px; flex-shrink: 0; font-size: 12px; color: var(--text-secondary); }
.security-number input { width: 100px; height: var(--control-height); padding-inline: 10px; background: var(--surface); color: var(--text-primary); border: 1px solid var(--border); border-radius: var(--control-radius); outline: none; }
.security-number input:focus { border-color: var(--border-strong); box-shadow: 0 0 0 2px var(--surface-active); }
.security-number input:disabled { opacity: .5; cursor: not-allowed; }
.security-switch { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 44px; min-height: 44px; flex-shrink: 0; cursor: pointer; }
.security-switch input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.security-switch span { width: 36px; height: 22px; border-radius: 999px; background: var(--border-strong); pointer-events: none; }
.security-switch span::after { content: ''; display: block; width: 16px; height: 16px; margin: 3px; border-radius: 50%; background: var(--surface); transition: transform 120ms ease; }
.security-switch input:checked + span { background: var(--text-primary); }
.security-switch input:checked + span::after { transform: translateX(14px); background: var(--bg); }
.security-switch input:focus-visible + span { outline: 2px solid var(--text-secondary); outline-offset: 3px; }
.security-switch input:disabled { cursor: not-allowed; }
.security-switch input:disabled + span { opacity: .4; }
.security-feedback { margin-bottom: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text-primary); font-size: 13px; line-height: 20px; }
.security-feedback.is-error, .security-field-copy .security-field-error { color: #b91c1c; }
:global(html.dark .security-feedback.is-error), :global(html.dark .security-field-copy .security-field-error) { color: #fca5a5; }
.security-number input[aria-invalid="true"] { border-color: #dc2626; }
.security-effective dl { margin-top: 12px; font-size: 12px; line-height: 20px; color: var(--text-secondary); }
.security-effective dl > div { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 12px; padding-block: 6px; }
.security-effective dd { overflow-wrap: anywhere; }
.security-loading { height: 240px; background: var(--surface); border-radius: 8px; }
@media (max-width: 640px) {
  .security-header { align-items: stretch; flex-direction: column; gap: 12px; }
  .security-actions { justify-content: flex-end; }
  .security-dirty { margin-right: auto; }
  .security-field { gap: 12px; }
  .security-number input { width: 76px; }
  .security-effective dl > div { grid-template-columns: 1fr; gap: 0; }
}
@media (prefers-reduced-motion: reduce) { .security-switch span::after { transition: none; } }
</style>
