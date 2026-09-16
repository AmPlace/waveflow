export const BUILTIN_CHINA_EPG_KEY = 'china_51zmt'

export function epgSourceDisplayName(source) {
  if (source?.source_origin === 'builtin' && source?.builtin_key === BUILTIN_CHINA_EPG_KEY) {
    const configuredName = String(source?.name || '').trim()
    return !configuredName || configuredName === '51zmt' ? '中国节目单' : configuredName
  }
  return String(source?.name || '未命名节目单')
}

export function epgSourceKindLabel(source) {
  return source?.source_origin === 'builtin' ? 'WaveFlow 内置' : '自定义'
}

export function epgSourceStatus(source) {
  const status = source?.enabled === false ? 'disabled' : String(source?.refresh?.status || 'failed')
  const states = {
    healthy: { key: 'healthy', label: '正常', detail: '节目单数据可正常使用' },
    stale: { key: 'stale', label: '使用旧数据', detail: '最近更新未完成，当前继续使用已有节目单' },
    failed: { key: 'failed', label: '更新失败', detail: '最近一次更新未成功' },
    disabled: { key: 'disabled', label: '已停用', detail: '已停止自动更新，已有数据仍可继续使用' },
  }
  return states[status] || states.failed
}

export function safeDisplayUrl(value) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    const parsed = new URL(text)
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`
  } catch {
    return text.split(/[?#]/, 1)[0]
  }
}

export function buildEpgSourceCreatePayload(draft) {
  return {
    name: String(draft?.name || '').trim(),
    url: String(draft?.url || '').trim(),
    enabled: draft?.enabled !== false,
  }
}

export function buildEpgSourceUpdatePayload(draft) {
  const payload = {
    name: String(draft?.name || '').trim(),
    enabled: draft?.enabled !== false,
  }
  if (draft?.replaceUrl) payload.url = String(draft?.url || '').trim()
  return payload
}

export function deleteNeedsAcknowledgement(impact) {
  return Boolean(
    impact?.requires_confirmation
      || Number(impact?.manual_bindings_count || 0) > 0
      || Number(impact?.locked_bindings_count || 0) > 0,
  )
}

export function formatCompactCount(value) {
  const count = Number(value || 0)
  return Number.isFinite(count) ? new Intl.NumberFormat('zh-CN').format(count) : '0'
}

export function formatEpgTime(value) {
  if (!value) return '尚无记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '尚无记录'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function formatCoverage(source) {
  const start = source?.data?.coverage_start
  const end = source?.data?.coverage_end
  if (!start && !end) return ''
  const formatter = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' })
  const format = (value) => {
    if (!value) return ''
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? '' : formatter.format(date)
  }
  const startText = format(start)
  const endText = format(end)
  if (startText && endText) return `${startText} – ${endText}`
  return startText || endText
}
