<template>
  <section class="market-automation" aria-labelledby="market-automation-title">
    <h3 id="market-automation-title">自动刷新</h3>
    <p v-if="loading" role="status">正在加载自动刷新设置…</p>
    <template v-else-if="snapshot">
      <form novalidate @submit.prevent="save">
        <label class="automation-toggle">
          <span>自动刷新 Market</span>
          <span class="automation-switch">
            <input v-model="enabled" type="checkbox" role="switch" :disabled="saving" aria-describedby="market-automation-help" />
            <span aria-hidden="true"></span>
          </span>
        </label>
        <p id="market-automation-help">刷新所有已启用的 Market 源，并更新已开启自动更新的包。不影响手动刷新；关闭不会中断正在执行的任务。</p>
        <div class="automation-interval">
          <label for="market-refresh-minutes">检查间隔</label>
          <div class="automation-number">
            <input id="market-refresh-minutes" v-model.number="minutes" type="number" step="any" :min="snapshot.minimum_interval_seconds / 60"
              :max="snapshot.maximum_interval_seconds == null ? undefined : snapshot.maximum_interval_seconds / 60"
              :disabled="saving || !enabled" :aria-invalid="Boolean(validationError)" aria-describedby="market-interval-help" />
            <span>分钟</span>
          </div>
        </div>
        <p id="market-interval-help">{{ rangeLabel }}{{ validationError ? '；' + validationError : '' }}</p>
        <p v-if="!snapshot.service_started" class="automation-warning" role="status">调度服务未运行，保存后需服务恢复才能自动执行。</p>
        <div class="automation-actions">
          <span v-if="dirty">有未保存的更改</span>
          <button type="submit" :disabled="saving || !dirty">{{ saving ? '保存中…' : '保存自动刷新' }}</button>
        </div>
      </form>
    </template>
    <p v-if="error" class="automation-error" role="alert">{{ error }}</p>
    <button v-if="!loading && !snapshot" type="button" @click="load">重新加载</button>
    <p v-if="success" role="status">{{ success }}</p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchMarketAutomation, updateMarketAutomation } from '../api/marketAutomation.js'
import { adminRequestErrorMessage } from '../api/adminUi.js'

const snapshot = ref(null)
const enabled = ref(false)
const minutes = ref(0)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const validationError = ref('')
const success = ref('')
let controller
let disposed = false
const dirty = computed(() => snapshot.value && (enabled.value !== snapshot.value.enabled
  || (enabled.value && minutes.value !== snapshot.value.interval_seconds / 60)))
const rangeLabel = computed(() => {
  const state = snapshot.value
  return state.maximum_interval_seconds == null
    ? `至少 ${state.minimum_interval_seconds / 60} 分钟`
    : `${state.minimum_interval_seconds / 60}–${state.maximum_interval_seconds / 60} 分钟（1 天 = 1440 分钟）`
})
function apply(data) {
  snapshot.value = data
  enabled.value = data.enabled
  minutes.value = data.interval_seconds / 60
}
async function load() {
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const data = await fetchMarketAutomation({ signal: request.signal })
    if (!disposed && !request.signal.aborted) apply(data)
  } catch (caught) {
    if (!disposed && !request.signal.aborted) error.value = adminRequestErrorMessage(caught, '自动刷新设置加载失败')
  } finally {
    if (!disposed && controller === request) loading.value = false
  }
}
async function save() {
  if (disposed || saving.value || !dirty.value) return
  error.value = ''; success.value = ''; validationError.value = ''
  const payload = {}
  if (enabled.value !== snapshot.value.enabled) payload.enabled = enabled.value
  if (enabled.value && minutes.value !== snapshot.value.interval_seconds / 60) {
    const seconds = Math.round(minutes.value * 60)
    if (typeof minutes.value !== 'number' || !Number.isFinite(minutes.value)
      || Math.abs(minutes.value * 60 - seconds) > 0.000001
      || seconds < snapshot.value.minimum_interval_seconds
      || (snapshot.value.maximum_interval_seconds != null && seconds > snapshot.value.maximum_interval_seconds)) {
      validationError.value = '请输入范围内的时间，精度不小于 1 秒'
      error.value = '请检查刷新间隔'
      return
    }
    payload.interval_seconds = seconds
  }
  controller?.abort()
  const request = new AbortController()
  controller = request
  saving.value = true
  try {
    const data = await updateMarketAutomation(payload, { signal: request.signal })
    if (!disposed && !request.signal.aborted) {
      apply(data)
      success.value = '自动刷新设置已保存'
    }
  } catch (caught) {
    if (!disposed && !request.signal.aborted) error.value = adminRequestErrorMessage(caught, '自动刷新设置保存失败')
  } finally {
    if (!disposed && controller === request) saving.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; controller?.abort() })
</script>

<style scoped>
.market-automation { padding-block: 16px; margin-bottom: 20px; border-block: 1px solid var(--border); color: var(--text-primary); }
h3 { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
p { font-size: 12px; line-height: 20px; color: var(--text-secondary); margin-block: 4px; }
.automation-toggle, .automation-interval { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }
.automation-interval { margin-top: 12px; }
.automation-number { display: flex; align-items: center; gap: 8px; }
.automation-number input { width: 100px; height: var(--control-height); border: 1px solid var(--border); border-radius: var(--control-radius); padding-inline: 10px; background: var(--surface); color: var(--text-primary); }
.automation-switch { position: relative; display: inline-flex; align-items: center; width: 44px; height: 44px; flex-shrink: 0; }
.automation-switch input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.automation-switch > span { width: 36px; height: 22px; border-radius: 999px; background: var(--border-strong); pointer-events: none; }
.automation-switch > span::after { content: ''; display: block; width: 16px; height: 16px; margin: 3px; border-radius: 50%; background: var(--surface); }
.automation-switch input:checked + span { background: var(--text-primary); }
.automation-switch input:checked + span::after { transform: translateX(14px); background: var(--bg); }
button { min-height: var(--control-height); padding-inline: 12px; border: 1px solid var(--border); border-radius: var(--control-radius); background: var(--surface); font-size: 12px; }
button:hover:not(:disabled) { background: var(--surface-hover); }
button:disabled, input:disabled, input:disabled + span { opacity: .45; cursor: not-allowed; }
button:focus-visible, .automation-number input:focus-visible, .automation-switch input:focus-visible + span { outline: 2px solid var(--text-secondary); outline-offset: 3px; }
.automation-actions { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; font-size: 12px; color: var(--text-secondary); }
.automation-error { color: #b91c1c; }
.automation-warning { color: #92400e; }
:global(html.dark .market-automation .automation-error) { color: #fca5a5; }
:global(html.dark .market-automation .automation-warning) { color: #fcd34d; }
</style>
