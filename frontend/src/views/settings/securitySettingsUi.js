export const SECURITY_GROUPS = Object.freeze([
  { key: 'access', title: '访客访问', description: '浏览与播放权限独立生效，管理设置始终需要登录。' },
  { key: 'credentials', title: '登录与播放凭证', description: '有效期用于新创建的会话或凭证，不会延长已有登录。' },
  { key: 'network', title: '网络来源与 RTSP', description: '影响服务器可以连接的媒体地址和 RTSP 转换资源。', advanced: true },
])

export const SECURITY_FIELDS = Object.freeze([
  { key: 'anonymous_browse', group: 'access', type: 'bool', label: '匿名浏览', description: '未登录访客可以浏览频道和节目。', risk: '访客浏览权限将改变。' },
  { key: 'anonymous_playback', group: 'access', type: 'bool', label: '匿名播放', description: '未登录访客可以播放，可能消耗服务器带宽。', risk: '访客播放权限与带宽使用范围将改变。' },
  { key: 'session_max_age_days', group: 'credentials', type: 'int', label: '登录有效期', unit: '天', description: '新登录会话的固定有效期。' },
  { key: 'media_credential_default_ttl_days', group: 'credentials', type: 'int', label: '播放凭证默认有效期', unit: '天', description: '未显式指定有效期时，新播放凭证使用此值。' },
  { key: 'allow_private', group: 'network', type: 'bool', label: '允许局域网媒体来源', description: '允许服务器访问私有网段中的媒体服务；不代表绕过所有地址安全检查。', risk: '服务器访问私有网络媒体地址的权限将改变。' },
  { key: 'allow_loopback', group: 'network', type: 'bool', label: '允许服务器本机媒体来源', description: '回环地址指 WaveFlow 服务器本机，不是浏览器所在设备。', risk: '服务器访问本机媒体服务的权限将改变。' },
  { key: 'enable_rtsp_proxy', group: 'network', type: 'bool', label: 'RTSP 浏览器播放转换', description: '使用服务器上的 FFmpeg 转换 RTSP，消耗服务器资源。', risk: 'RTSP 转换可用性将改变，可能影响播放请求。' },
  { key: 'rtsp_max_sessions', group: 'network', type: 'int', label: 'RTSP 并发会话上限', unit: '个', description: '限制新会话准入，不会主动关闭已有会话。', dependsOn: 'enable_rtsp_proxy' },
])

// Persisted schema entries without verified production consumers are not user controls.
export const HIDDEN_RUNTIME_FIELDS = Object.freeze([
  'public_base_url', 'probe_concurrency', 'subscription_refresh_cooldown',
  'm3u8_cache_ttl', 'm3u8_cache_max_entries',
])

export function settingsDraft(data) {
  const forced = new Set(data?.forced || [])
  return Object.fromEntries(SECURITY_FIELDS.map(({ key }) => [key,
    forced.has(key) ? data?.settings?.[key] : (data?.runtime?.[key] ?? data?.settings?.[key]),
  ]))
}

export function fieldAvailable(item, schema) {
  return schema[item.key]?.type === item.type
}

export function settingsPatch(draft, baseline, forced, schema) {
  return Object.fromEntries(SECURITY_FIELDS
    .filter(item => fieldAvailable(item, schema) && !forced.has(item.key)
      && (!item.dependsOn || draft[item.dependsOn] === true)
      && draft[item.key] !== baseline[item.key])
    .map(item => [item.key, draft[item.key]]))
}

export function validateSettingsPatch(payload, schema) {
  const errors = {}
  for (const item of SECURITY_FIELDS) {
    if (!Object.hasOwn(payload, item.key)) continue
    const value = payload[item.key]
    const bounds = schema[item.key] || {}
    if (item.type === 'bool' && typeof value !== 'boolean') errors[item.key] = `请选择${item.label}`
    if (item.type === 'int' && (!Number.isInteger(value) || value < bounds.min || value > bounds.max)) {
      errors[item.key] = `${item.label}请输入 ${bounds.min}–${bounds.max} ${item.unit}之间的整数`
    }
  }
  return errors
}

export function settingsRisks(payload) {
  return SECURITY_FIELDS.filter(item => item.risk && Object.hasOwn(payload, item.key))
    .map(item => `${item.label}：${payload[item.key] ? '开启' : '关闭'}。${item.risk}`)
}
