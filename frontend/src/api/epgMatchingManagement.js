import { apiRequest } from './client.js'
import { adminRequestErrorMessage } from './adminUi.js'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

function queryString(values) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

export async function fetchEpgMatchingOverview({ signal } = {}) {
  const response = await apiRequest('/api/admin/epg/overview', { signal })
  return response.json()
}

export async function fetchEpgMatchingChannels({
  page = 1,
  pageSize = 30,
  scope = 'all',
  logicalScope = 'active',
  diagnosticStatus = '',
  sourceId = null,
  query = '',
  signal,
} = {}) {
  const suffix = queryString({
    page,
    page_size: pageSize,
    scope,
    logical_scope: logicalScope,
    diagnostic_status: diagnosticStatus,
    source_id: sourceId,
    q: query,
  })
  const response = await apiRequest(`/api/admin/epg/matching${suffix}`, { signal })
  return response.json()
}

export async function fetchEpgMatchingDetail(logicalChannelId, {
  candidateLimit = 10,
  signal,
} = {}) {
  const suffix = queryString({ candidate_limit: candidateLimit })
  const response = await apiRequest(
    `/api/admin/epg/matching/${encodeURIComponent(logicalChannelId)}${suffix}`,
    { signal },
  )
  return response.json()
}

export async function searchEpgChannelCatalog({
  page = 1,
  pageSize = 20,
  query = '',
  sourceId = null,
  availability = 'all',
  signal,
} = {}) {
  const suffix = queryString({
    page,
    page_size: pageSize,
    q: query,
    source_id: sourceId,
    availability,
  })
  const response = await apiRequest(`/api/admin/epg/catalog${suffix}`, { signal })
  return response.json()
}

export async function setManualEpgBinding(logicalChannelId, identity) {
  const response = await apiRequest(
    `/api/admin/epg/logical-channels/${encodeURIComponent(logicalChannelId)}/binding`,
    {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({
        epg_source_id: identity?.epg_source_id,
        epg_channel_id: identity?.epg_channel_id,
      }),
    },
  )
  return response.json()
}

export async function lockEpgBinding(logicalChannelId) {
  const response = await apiRequest(
    `/api/admin/epg/logical-channels/${encodeURIComponent(logicalChannelId)}/binding/lock`,
    { method: 'PUT' },
  )
  return response.json()
}

export async function unlockEpgBinding(logicalChannelId) {
  const response = await apiRequest(
    `/api/admin/epg/logical-channels/${encodeURIComponent(logicalChannelId)}/binding/lock`,
    { method: 'DELETE' },
  )
  return response.json()
}

export async function restoreAutomaticEpgBinding(logicalChannelId) {
  const response = await apiRequest(
    `/api/admin/epg/logical-channels/${encodeURIComponent(logicalChannelId)}/restore-automatic`,
    { method: 'POST', timeout: 120_000 },
  )
  return response.json()
}

export async function disableLogicalChannelEpg(logicalChannelId) {
  const response = await apiRequest(
    `/api/admin/epg/logical-channels/${encodeURIComponent(logicalChannelId)}/no-epg`,
    { method: 'PUT' },
  )
  return response.json()
}

export function epgMatchingErrorCode(error) {
  const payload = error?.detail?.detail || error?.detail || null
  return typeof payload === 'object' && payload ? String(payload.code || '') : ''
}

export function epgMatchingErrorMessage(error, fallback = '操作失败，请稍后重试') {
  const messages = {
    logical_channel_not_found: '频道已不存在，请刷新列表',
    logical_channel_conflict: '频道来源仍有冲突，请先处理直播源',
    logical_channel_inactive: '这个历史频道当前不能修改',
    epg_source_not_found: '所选节目单来源已不存在',
    epg_channel_not_found: '所选节目单频道已不存在',
    invalid_composite_target: '所选节目单频道已发生变化，请重新选择',
    binding_conflict: '当前频道没有可执行此操作的节目单绑定',
    orphan_target: '原节目单已不可用，请更换节目单或恢复自动匹配',
    binding_write_failed: '节目单设置暂时无法保存，请稍后重试',
    invalid_request: '提交内容无效，请刷新后重试',
  }
  return messages[epgMatchingErrorCode(error)] || adminRequestErrorMessage(error, fallback)
}
