import { apiRequest } from './client'
import { adminRequestErrorMessage } from './adminUi.js'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export async function fetchEpgSources({ signal } = {}) {
  const response = await apiRequest('/api/admin/epg/sources', { signal })
  return response.json()
}

export async function createEpgSource(payload) {
  const response = await apiRequest('/api/admin/epg/sources', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  return response.json()
}

export async function updateEpgSource(sourceId, payload) {
  const response = await apiRequest(`/api/admin/epg/sources/${encodeURIComponent(sourceId)}`, {
    method: 'PATCH',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })
  return response.json()
}

export async function refreshEpgSource(sourceId) {
  const response = await apiRequest(`/api/admin/epg/sources/${encodeURIComponent(sourceId)}/refresh`, {
    method: 'POST',
    timeout: 120_000,
  })
  return response.json()
}

export async function fetchEpgSourceDeleteImpact(sourceId, { signal } = {}) {
  const response = await apiRequest(`/api/admin/epg/sources/${encodeURIComponent(sourceId)}/delete-impact`, { signal })
  return response.json()
}

export async function deleteEpgSource(sourceId, { confirm = false } = {}) {
  const suffix = confirm ? '?confirm=true' : ''
  const response = await apiRequest(`/api/admin/epg/sources/${encodeURIComponent(sourceId)}${suffix}`, {
    method: 'DELETE',
  })
  return response.json()
}

export function epgSourceErrorCode(error) {
  const payload = error?.detail?.detail || error?.detail || null
  return typeof payload === 'object' && payload ? String(payload.code || '') : ''
}

export function epgSourceErrorMessage(error, fallback = '操作失败，请稍后重试') {
  const messages = {
    source_not_found: '节目单来源不存在或已被删除',
    builtin_delete_forbidden: 'WaveFlow 内置节目单不能删除',
    builtin_url_managed: 'WaveFlow 内置节目单地址由系统维护',
    invalid_name: '请输入有效的来源名称',
    invalid_url: '请输入有效的 HTTP 或 HTTPS XMLTV 地址',
    invalid_enabled: '启用状态无效，请刷新页面后重试',
    source_url_exists: '这个 XMLTV 地址已经添加过了',
    delete_confirmation_required: '这个来源仍有受保护的频道绑定，请确认影响后再删除',
    source_disabled: '请先启用该节目单来源，再执行刷新',
    source_refresh_busy: '这个节目单正在刷新，请稍后再试',
    automation_unavailable: '刷新服务暂时不可用，请稍后再试',
    refresh_unavailable: '节目单刷新暂时不可用，请稍后再试',
    empty_update: '没有需要保存的修改',
    invalid_request: '提交内容无效，请检查后重试',
  }
  return messages[epgSourceErrorCode(error)] || adminRequestErrorMessage(error, fallback)
}
