export const ADAPTER_SCHEMES = Object.freeze([
  'youtube',
  'migu', 'hnntv', 'nmtv', 'gzstv', 'sxbc', 'xjtv', 'jstv', 'sdtv', 'sdly',
  'douyin', 'douyu', 'huya', 'hbtv', 'hntv', 'tvb', 'nowtv',
  'redbook', 'tiktok', 'kuaishou', 'bilibili', 'yy', 'bigo', 'blued', 'soop',
  'netease', 'pandatv', 'maoer', 'look', 'flextv', 'popkontv', 'twitcasting',
  'baidu', 'weibo', 'kugou', 'twitch', 'huajiao', 'showroom', 'inke', 'acfun',
  'zhihu', 'chzzk', 'live17', 'langlive', 'changliao',
  'jd', 'faceit', 'lianjie', 'sixroom', 'huamao', 'shopee', 'laixiu', 'picarto',
  'fjtv', 'ptbtv', 'nd0593tv', 'qukan', 'woniu',
  'adapter',
])

export function isAdapterSchemeUrl(url) {
  const value = String(url || '').trim().toLowerCase()
  return ADAPTER_SCHEMES.some((scheme) => value.startsWith(`${scheme}://`))
}

export function adapterNameFromUrl(url) {
  const value = String(url || '').trim().toLowerCase()
  for (const scheme of ADAPTER_SCHEMES) {
    if (scheme === 'adapter') continue
    if (value.startsWith(`${scheme}://`)) return scheme
  }
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'adapter:' ? parsed.hostname.toLowerCase() : ''
  } catch {
    return ''
  }
}

export function isProxyTransport(entry) {
  return entry?.type === 'proxy' || Boolean(entry?.via_proxy)
}

export function sourceTransport(entry) {
  return isProxyTransport(entry) ? 'proxy' : 'direct'
}

export function sourceIdentity(entry) {
  return String(entry?.source_id || '').trim()
}

export function channelIdentity(channel) {
  const logicalId = String(channel?.logical_channel_id || '').trim()
  if (logicalId) return `logical:${logicalId}`
  const canonicalKey = String(channel?.canonical_key || '').trim()
  if (canonicalKey) return canonicalKey
  const name = String(channel?.name || '').trim()
  return name ? `name:${name}` : ''
}

export function sourceRaceKey(entry) {
  const sourceId = sourceIdentity(entry)
  if (sourceId) return `${sourceId}:${sourceTransport(entry)}`
  const fallback = String(entry?.original_url || entry?.url || '').trim()
  return fallback ? `${fallback}:${sourceTransport(entry)}` : ''
}

export function buildChannelProxyUrl({ apiBase, channelKey, sourceId = '', expectedSourceRevision = '', accessToken = '' }) {
  const key = String(channelKey || '').trim()
  if (!key) return ''
  const params = new URLSearchParams()
  const sid = String(sourceId || '').trim()
  if (sid) params.set('source_id', sid)
  const revision = String(expectedSourceRevision || '').trim()
  if (revision) params.set('expected_source_revision', revision)
  if (accessToken) params.set('access_token', accessToken)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return `${apiBase}/api/media/channel/${encodeURIComponent(key)}/playlist.m3u8${suffix}`
}

export function extractSourceIdFromUrl(url) {
  try {
    const parsed = new URL(url, 'http://waveflow.local')
    return parsed.searchParams.get('source_id') || ''
  } catch {
    return ''
  }
}

export function isDynamicAdapterProxyPlaylistEntry(entry, isChannelProxyPlaylistUrl) {
  return Boolean(
    isProxyTransport(entry)
    && typeof isChannelProxyPlaylistUrl === 'function'
    && isChannelProxyPlaylistUrl(entry?.url || '')
    && (entry?.adapter || isAdapterSchemeUrl(entry?.original_url || entry?.url || '')),
  )
}

export function isSourceExplicitlyDisabled(entry) {
  if (entry?.disabled === true) return true
  const enabled = entry?.enabled
  if (enabled === false || enabled === 0) return true
  return typeof enabled === 'string' && ['0', 'false', 'no', 'off'].includes(enabled.trim().toLowerCase())
}

export function startupRaceCandidateKind(entry, {
  sourceType = '',
  hlsSupported = false,
  mpegTsSupported = false,
} = {}) {
  if (!entry?.url || isSourceExplicitlyDisabled(entry)) return ''
  if (isProxyTransport(entry) && entry.adapter_transport_pending) return 'proxy_auto'
  if (sourceType === 'unsupported_youtube_url' || sourceType === 'youtube') return ''
  if (sourceType === 'hls' && hlsSupported) return 'hls'
  if (['mpegts', 'http_flv'].includes(sourceType) && mpegTsSupported) return sourceType
  return ''
}

function isUrlEntryNotLive(u) {
  return String(u?.probe_status || '').toLowerCase() === 'not_live'
}

// 测速状态只用于排序和提示；只有配置明确禁用的 source 才阻止用户尝试。
export function isChannelAllUrlsBlocked(channel) {
  const urls = channel?.urls
  if (!Array.isArray(urls) || urls.length === 0) return false
  return urls.every(isSourceExplicitlyDisabled)
}

export function isChannelAllUnsupported(channel) {
  const urls = channel?.urls
  if (!Array.isArray(urls) || urls.length === 0) return false
  return urls.every((entry) => {
    if (isSourceExplicitlyDisabled(entry)) return true
    const sourceType = String(entry?.source_type || entry?.transport || '').trim().toLowerCase()
    return sourceType === 'unsupported' || sourceType === 'unsupported_youtube_url'
  })
}

// 频道是否所有 source 都是 not_live。仅用于显示提示文案，不用于禁止点击。
export function isChannelAllNotLive(channel) {
  const urls = channel?.urls
  if (!Array.isArray(urls) || urls.length === 0) return false
  return urls.every(isUrlEntryNotLive)
}
