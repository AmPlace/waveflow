const DIAGNOSTIC_STATES = {
  healthy_bound: {
    key: 'matched', label: '已匹配', tone: 'success', detail: '频道已有可用节目单',
  },
  unmatched: {
    key: 'unmatched', label: '未匹配', tone: 'neutral', detail: '当前没有节目单，不影响频道播放',
  },
  ambiguous: {
    key: 'attention', label: '多个候选', tone: 'warning', detail: '找到多个可能的节目单，需要选择一个',
  },
  logical_conflict: {
    key: 'attention', label: '需要处理', tone: 'warning', detail: '频道来源存在冲突',
  },
  split_conflict: {
    key: 'attention', label: '需要处理', tone: 'warning', detail: '频道来源发生拆分冲突',
  },
  merge_conflict: {
    key: 'attention', label: '需要处理', tone: 'warning', detail: '频道来源发生合并冲突',
  },
  preference_conflict: {
    key: 'attention', label: '需要处理', tone: 'warning', detail: '节目单来源偏好存在冲突',
  },
  missing_target: {
    key: 'attention', label: '原节目单不可用', tone: 'warning', detail: '原节目单来源或频道已不存在',
  },
  logical_orphan: {
    key: 'inactive', label: '历史频道', tone: 'muted', detail: '频道已经没有可用直播源',
  },
  not_applicable: {
    key: 'disabled', label: '不使用节目单', tone: 'muted', detail: '已明确关闭这个频道的节目单',
  },
}

const DEFAULT_STATE = DIAGNOSTIC_STATES.unmatched

export const MATCHING_SCOPE_OPTIONS = Object.freeze([
  { key: 'all', label: '全部' },
  { key: 'bound', label: '已匹配' },
  { key: 'unbound', label: '未匹配' },
  { key: 'needs_attention', label: '需处理' },
])

export function epgMatchingState(item) {
  const status = String(item?.diagnostic?.status || '')
  return DIAGNOSTIC_STATES[status] || DEFAULT_STATE
}

export function epgMatchingTarget(item, sourceName = '') {
  const state = epgMatchingState(item)
  const binding = item?.binding || {}
  if (state.key === 'disabled') return '不使用节目单'
  if (state.key === 'inactive') return '频道已失效'
  if (String(item?.diagnostic?.status || '') === 'missing_target') {
    return '原节目单来源已不可用'
  }
  if (binding.status === 'bound') {
    const source = String(sourceName || binding.epg_source_name || '').trim() || '未知节目单'
    const channel = String(binding.epg_channel_display_name || binding.epg_channel_id || '').trim() || '未知频道'
    return `${source} · ${channel}`
  }
  if (state.label === '多个候选') return '找到多个可能的节目单'
  if (state.key === 'attention') return state.detail
  return '暂无节目单'
}

export function epgBindingOriginLabel(origin) {
  const labels = {
    automatic: '自动匹配',
    manual: '手动绑定',
    legacy_migrated: '历史迁移',
  }
  return labels[String(origin || '')] || ''
}

export function epgLogicalState(item) {
  const state = String(item?.channel?.state || '')
  if (state === 'active') return { manageable: true, label: '正常频道' }
  if (state === 'orphaned') return { manageable: false, label: '历史频道' }
  if (state === 'split_conflict' || state === 'merge_conflict') {
    return { manageable: false, label: '频道来源冲突' }
  }
  return { manageable: false, label: '当前不可管理' }
}

export function epgPreferenceSummary(preference) {
  const status = String(preference?.status || 'none')
  const sourceName = String(preference?.preferred_source_name || '').trim()
  const origins = {
    manual: '手动设置',
    subscription: '直播源关联',
    market: 'Market 来源',
    url_tvg: '直播源声明',
  }
  if (status === 'preferred') {
    return {
      label: sourceName || '已设置来源偏好',
      detail: origins[String(preference?.origin || '')] || '来源偏好',
      tone: 'normal',
    }
  }
  if (status === 'conflict') return { label: '来源偏好存在冲突', detail: '需要在直播源设置中处理', tone: 'warning' }
  if (status === 'stale_preference') return { label: sourceName || '偏好来源暂不可用', detail: '偏好仍保留', tone: 'warning' }
  if (status === 'missing_source') return { label: '偏好来源已不存在', detail: '不会自动选择其他来源', tone: 'warning' }
  return { label: '未设置来源偏好', detail: '', tone: 'muted' }
}

export function epgCatalogTarget(item) {
  return {
    epg_source_id: Number(item?.identity?.epg_source_id ?? item?.epg_source_id),
    epg_channel_id: String(item?.identity?.epg_channel_id ?? item?.epg_channel_id ?? ''),
  }
}

export function epgCatalogDisplayName(item) {
  return String(item?.epg_channel_display_name || item?.epg_channel_id || '未命名 EPG 频道')
}

export function epgSourceHealthLabel(value) {
  const labels = {
    healthy: '正常',
    stale: '使用旧数据',
    failed: '更新失败',
    disabled: '已停用',
  }
  return labels[String(value || '')] || ''
}

export function epgScopeCount(overview, scope) {
  const channels = overview?.logical_channels || {}
  if (scope === 'bound') return Number(channels.bound || 0)
  if (scope === 'unbound') return Number(channels.unbound || 0)
  if (scope === 'needs_attention') return Number(channels.needs_attention || 0)
  return Number(channels.total || 0)
}
