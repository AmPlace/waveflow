export const SETTINGS_TABS = Object.freeze([
  { key: 'sources', label: '直播源', to: '/settings/sources' },
  { key: 'epg', label: 'EPG', to: '/settings/epg/sources' },
  { key: 'plugins', label: 'Plugins', to: '/settings/plugins' },
  { key: 'security', label: '安全与访问', to: '/settings/security' },
])

export const EPG_SETTINGS_TABS = Object.freeze([
  { key: 'sources', label: '来源', to: '/settings/epg/sources' },
  { key: 'matching', label: '频道匹配', to: '/settings/epg/matching' },
])

export function activeSettingsTab(path) {
  if (String(path).startsWith('/settings/epg')) return 'epg'
  if (String(path).startsWith('/settings/plugins')) return 'plugins'
  if (String(path).startsWith('/settings/security')) return 'security'
  return 'sources'
}

export function activeEpgSettingsTab(path) {
  if (String(path).startsWith('/settings/epg/matching')) return 'matching'
  return 'sources'
}
