export const IPTV_CHANNEL_SORT_MODES = Object.freeze([
  { key: 'original', label: '默认排序' },
  { key: 'natural', label: 'A-Z排序' },
  { key: 'group', label: '分组排序' },
])

const IPTV_CHANNEL_SORT_KEYS = new Set(IPTV_CHANNEL_SORT_MODES.map((mode) => mode.key))

export function normalizeIptvChannelSortMode(mode) {
  return IPTV_CHANNEL_SORT_KEYS.has(mode) ? mode : 'original'
}

function naturalSort(a, b) {
  return String(a || '').localeCompare(String(b || ''), undefined, {
    numeric: true,
    sensitivity: 'base',
  })
}

export function sortIptvChannels(channels, mode) {
  const result = Array.isArray(channels) ? channels.slice() : []
  const normalizedMode = normalizeIptvChannelSortMode(mode)
  if (normalizedMode === 'natural') {
    result.sort((a, b) => naturalSort(a?.name, b?.name))
  } else if (normalizedMode === 'group') {
    result.sort((a, b) => naturalSort(a?.group_name, b?.group_name) || naturalSort(a?.name, b?.name))
  }
  return result
}
