import { ref } from 'vue'
import { API_BASE } from '../apiBase.js'

async function request(url, options = {}) {
  const { timeout = 10_000, signal, ...fetchOptions } = options
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  const onAbort = () => ctrl.abort()
  if (signal) {
    if (signal.aborted) ctrl.abort()
    else signal.addEventListener('abort', onAbort, { once: true })
  }
  try {
    const res = await fetch(`${API_BASE}${url}`, { ...fetchOptions, signal: ctrl.signal })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  } catch (e) {
    throw e
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener?.('abort', onAbort)
  }
}

export function useEpg({
  getCurrentChannelKey = null,
  getCurrentDate = null,
} = {}) {
  const current = ref(null)
  const next = ref(null)
  const schedule = ref([])
  const selectedDate = ref('')
  const availableDates = ref([])
  const loading = ref(false)
  const error = ref('')

  let requestSeq = 0
  let activeRequest = null

  function normalizeKey(value) {
    return String(value || '').trim()
  }

  function normalizeDate(value) {
    return String(value || '').trim()
  }

  function requestIdentity(canonicalKey, options = {}) {
    return {
      seq: ++requestSeq,
      channelKey: normalizeKey(canonicalKey),
      date: normalizeDate(options.date),
      controller: new AbortController(),
    }
  }

  function beginProgramsRequest(canonicalKey, options = {}) {
    activeRequest?.controller?.abort()
    activeRequest = requestIdentity(canonicalKey, options)
    return activeRequest
  }

  function isCurrentProgramsRequest(identity) {
    if (!identity || activeRequest !== identity) return false
    if (identity.seq !== requestSeq) return false
    if (getCurrentChannelKey && normalizeKey(getCurrentChannelKey()) !== identity.channelKey) return false
    if (getCurrentDate && normalizeDate(getCurrentDate()) !== identity.date) return false
    return true
  }

  function invalidatePrograms({ resetLoading = true } = {}) {
    requestSeq += 1
    activeRequest?.controller?.abort()
    activeRequest = null
    if (resetLoading) loading.value = false
  }

  function clearPrograms() {
    invalidatePrograms()
    current.value = null
    next.value = null
    schedule.value = []
    selectedDate.value = ''
    availableDates.value = []
    error.value = ''
  }

  async function fetchPrograms(canonicalKey, options = {}) {
    if (!canonicalKey) return { applied: false, stale: true }
    const identity = beginProgramsRequest(canonicalKey, options)
    loading.value = true
    error.value = ''
    try {
      const params = new URLSearchParams()
      const tz = options.tz || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'
      if (options.date) params.set('date', options.date)
      if (tz) params.set('tz', tz)
      const qs = params.toString()
      const data = await request(
        `/api/iptv/epg/programs/${encodeURIComponent(canonicalKey)}${qs ? `?${qs}` : ''}`,
        { signal: identity.controller.signal },
      )
      if (!isCurrentProgramsRequest(identity)) return { applied: false, stale: true }
      current.value = data.current || null
      next.value = data.next || null
      schedule.value = data.programs || []
      selectedDate.value = data.date || options.date || ''
      availableDates.value = data.available_dates || []
      error.value = ''
      return { applied: true, stale: false, current: current.value, next: next.value }
    } catch (e) {
      if (!isCurrentProgramsRequest(identity)) return { applied: false, stale: true }
      console.warn('[EPG] fetch failed:', e?.message)
      current.value = null
      next.value = null
      schedule.value = []
      selectedDate.value = options.date || ''
      availableDates.value = []
      error.value = e?.message || 'EPG 加载失败'
      return { applied: true, stale: false, error: e }
    } finally {
      if (isCurrentProgramsRequest(identity)) loading.value = false
    }
  }

  async function batchCurrent(canonicalKeys, { signal } = {}) {
    if (!canonicalKeys.length) return {}
    try {
      return await request('/api/iptv/epg/batch-current', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ canonical_keys: canonicalKeys }),
        signal,
        timeout: 8_000,
      })
    } catch (e) {
      if (e?.name !== 'AbortError') console.warn('[EPG] batch fetch failed:', e?.message)
      return null
    }
  }

  return {
    availableDates,
    batchCurrent,
    clearPrograms,
    current,
    error,
    fetchPrograms,
    invalidatePrograms,
    loading,
    next,
    schedule,
    selectedDate,
  }
}
