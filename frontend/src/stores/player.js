import { defineStore } from 'pinia'
import { API_BASE } from '../apiBase.js'
import { adapterNameFromUrl, buildChannelProxyUrl, channelIdentity, isAdapterSchemeUrl, isSourceExplicitlyDisabled, sourceRaceKey } from '../utils/sourceIdentity.js'
import { normalizeIptvChannelSortMode } from '../utils/iptvChannelList.js'
import { currentRadioProgramme } from '../utils/radioProgramme.js'

function normalizeChannelContextChannels(channels) {
  const result = []
  const seen = new Set()
  for (const channel of Array.isArray(channels) ? channels : []) {
    const identity = channelIdentity(channel)
    if (!identity || seen.has(identity)) continue
    seen.add(identity)
    result.push(channel)
  }
  return result
}

// A channel switch must invalidate and cancel adapter resolves belonging to the
// previous selection.  The selection token remains the authoritative stale
// result guard; aborting here also avoids keeping provider requests alive after
// the user has already moved to another channel.
const activeIptvResolveControllers = new Set()

const RADIO_PLAYBACK_INTENTS = new Set([
  'station_click',
  'play_button',
  'source_switch',
  'passive',
  'recovery',
])

function normalizeRadioPlaybackIntent(intent, fallback = 'passive') {
  const value = String(intent || '').trim()
  return RADIO_PLAYBACK_INTENTS.has(value) ? value : fallback
}

function abortActiveIptvResolves() {
  for (const controller of activeIptvResolveControllers) {
    try { controller.abort() } catch {}
  }
  activeIptvResolveControllers.clear()
}

export const usePlayerStore = defineStore('player', {
  state: () => ({
    isPlaying: false,
    isLoading: false,
    isMuted: false,
    isMutedInitialized: false,
    currentStation: '',
    radioPlaybackIntent: 'passive',
    volume: 1,
    playbackError: '',
    stationList: [],
    stationMap: {},
    isPlayerExpanded: false,
    activeMode: 'radio',  // 'radio' | 'iptv'
    // IPTV 播放状态
    currentIptvChannel: null,  // { name, group_name, logo_url, urls: [...] }
    pendingIptvChannel: null,  // 正在异步构建播放队列的频道，用于立即反馈选中态
    iptvUrls: [],              // 当前频道的所有可用链接
    iptvUrlIndex: 0,           // 当前尝试的链接索引
    iptvSelectionToken: 0,      // 防止异步解析旧频道覆盖新频道
    iptvVideoEl: null,         // FullPlayer 中的 video 元素引用（iOS 同步播放用）
    currentEpgProgram: null,   // EPG: { title, start, stop, progress, remaining_minutes }
    iptvChannelSortMode: 'original',
    iptvChannelContext: null,  // { token, origin, group, search, channels }
    iptvChannelContextToken: 0,
  }),

  actions: {
    stopAndClearPlayback() {
      abortActiveIptvResolves()
      ++this.iptvSelectionToken
      this.currentStation = ''
      this.radioPlaybackIntent = 'passive'
      this.currentIptvChannel = null
      this.pendingIptvChannel = null
      this.iptvUrls = []
      this.iptvUrlIndex = 0
      this.iptvVideoEl = null
      this.currentEpgProgram = null
      this.iptvChannelContext = null
      ++this.iptvChannelContextToken
      this.isPlayerExpanded = false
      this.isPlaying = false
      this.isLoading = false
      this.playbackError = ''
    },

    // stationId 对应后端路由中的 {station_id}
    switchStation(stationId) {
      if (!stationId) {
        return
      }

      // 使旧的 playIptvChannel 异步 resolve 失效
      abortActiveIptvResolves()
      ++this.iptvSelectionToken

      // 停止 IPTV 播放
      this.currentIptvChannel = null
      this.pendingIptvChannel = null
      this.iptvUrls = []
      this.iptvUrlIndex = 0
      this.currentStation = stationId
      this.radioPlaybackIntent = 'station_click'

      this.playbackError = ''

      this.isLoading = true

      this.isPlaying = true
    },


    togglePlay(forcePlaying, options = {}) {
      const hasForcedValue = typeof forcePlaying === 'boolean'
      const nextPlaying = hasForcedValue ? forcePlaying : !this.isPlaying

      if (nextPlaying && this.currentStation && !this.currentIptvChannel) {
        this.radioPlaybackIntent = normalizeRadioPlaybackIntent(options?.intent, 'play_button')
      } else if (options?.intent) {
        this.radioPlaybackIntent = normalizeRadioPlaybackIntent(options.intent)
      }

      this.isPlaying = nextPlaying
    },

    consumeRadioPlaybackIntent(fallback = 'passive') {
      const intent = normalizeRadioPlaybackIntent(this.radioPlaybackIntent, fallback)
      this.radioPlaybackIntent = normalizeRadioPlaybackIntent(fallback)
      return intent
    },

    setLoading(nextLoading) {
      this.isLoading = Boolean(nextLoading)
    },

    initializeMuted(defaultMuted = false) {
      if (this.isMutedInitialized) return
      this.isMuted = Boolean(defaultMuted)
      this.isMutedInitialized = true
    },

    setMuted(nextMuted) {
      this.isMuted = Boolean(nextMuted)
      this.isMutedInitialized = true
    },

    setVolume(volumeValue) {
      const nextVolume = Number(volumeValue)

      if (Number.isNaN(nextVolume)) {
        return
      }

      this.volume = Math.min(1, Math.max(0, nextVolume))
    },

    setPlaybackError(message) {
      this.playbackError = message ? String(message) : ''

      if (this.playbackError) {
        this.isLoading = false
      }
    },

    clearPlaybackError() {
      this.playbackError = ''
    },

    addRadioStations(stations) {
      if (!Array.isArray(stations)) return false
      const next = [...this.stationList]
      const merged = { ...this.stationMap }
      const incomingIds = new Set()
      for (const station of stations) {
        if (!station?.id) continue
        incomingIds.add(station.id)
        const existing = merged[station.id]
        const selectedSource = existing?.radioSourceId
        const sourceStillAvailable = selectedSource && (station.radioSources || []).some(
          (source) => source.source_id === selectedSource,
        )
        const updated = existing
          ? {
              ...existing,
              ...station,
              catalogRemoved: false,
              radioSourceId: sourceStillAvailable ? selectedSource : station.radioSourceId,
              radioProgrammes: existing.radioProgrammes,
            }
          : { ...station, catalogRemoved: false }
        merged[station.id] = updated
        const index = next.findIndex((item) => item.id === station.id)
        if (index >= 0) next[index] = updated
        else next.push(updated)
      }
      // Radio Home has one catalog authority. A successful snapshot removes
      // rows that no enabled provider still publishes, except an active
      // playback projection which remains until the attempt finishes.
      for (const [stationId, station] of Object.entries(merged)) {
        if (station?.radioDomain && !incomingIds.has(stationId)) {
          if (stationId === this.currentStation) {
            merged[stationId] = { ...station, catalogRemoved: true }
            const index = next.findIndex((item) => item.id === stationId)
            if (index >= 0) next[index] = merged[stationId]
          } else {
            delete merged[stationId]
          }
        }
      }
      this.stationList = next.filter((station) => (
        incomingIds.has(station.id) || station.id === this.currentStation
      ))
      this.stationMap = merged
      return true
    },

    updateRadioProgramme(stationId, programmes) {
      const station = this.stationMap[stationId]
      if (!station || !station.radioStationId) return
      const items = Array.isArray(programmes) ? programmes : []
      station.radioProgrammes = items
      const current = currentRadioProgramme(items)
      if (current) {
        station.subtitle = String(current.subtitle || current.title || '')
      }
    },

    selectRadioSource(stationId, sourceId) {
      const station = this.stationMap[stationId]
      const wanted = String(sourceId || '').trim()
      if (!station?.radioStationId || !wanted) return false
      const source = (station.radioSources || []).find((item) => item?.source_id === wanted)
      if (!source) return false
      station.radioSourceId = wanted
      const listItem = this.stationList.find((item) => item.id === stationId)
      if (listItem) listItem.radioSourceId = wanted
      if (this.currentStation === stationId && this.isPlaying) {
        this.isLoading = true
        this.radioPlaybackIntent = 'source_switch'
      }
      return true
    },

    expandPlayer() {
      this.isPlayerExpanded = true
    },

    collapsePlayer() {
      this.isPlayerExpanded = false
    },

    setActiveMode(mode) {
      this.activeMode = mode
    },

    setIptvChannelSortMode(mode) {
      this.iptvChannelSortMode = normalizeIptvChannelSortMode(mode)
    },

    setIptvChannelContext({ origin = 'iptv-home', group = '', search = '', channels = [] } = {}) {
      const token = ++this.iptvChannelContextToken
      this.iptvChannelContext = {
        token,
        origin: String(origin || 'iptv-home'),
        group: String(group || ''),
        search: String(search || ''),
        channels: normalizeChannelContextChannels(channels),
      }
      return token
    },

    refreshIptvChannelContext({ token = 0, group = '', search = '', channels = [] } = {}) {
      const current = this.iptvChannelContext
      if (!current) return false
      if (token && current.token !== token) return false
      if (current.group !== String(group || '') || current.search !== String(search || '')) return false
      this.iptvChannelContext = {
        ...current,
        channels: normalizeChannelContextChannels(channels),
      }
      return true
    },

    async playIptvChannel(channel, options = {}) {
      abortActiveIptvResolves()
      const selectionToken = ++this.iptvSelectionToken
      if (Object.prototype.hasOwnProperty.call(options, 'channelContext')) {
        const context = options.channelContext
        if (context) this.setIptvChannelContext(context)
        else {
          this.iptvChannelContext = null
          ++this.iptvChannelContextToken
        }
      }
      // 停止电台播放，触发 AudioEngine destroyHls
      this.currentStation = ''
      this.pendingIptvChannel = channel
      this.playbackError = ''
      this.isLoading = true
      try {
      const sorted = [...channel.urls].sort((a, b) => {
        const rank = (u) => {
          const status = String(u?.probe_status || '').trim().toLowerCase()
          if (u?.is_working === 1 || status === 'online') return 0
          if (!status || status === 'untested' || status === 'unknown' || u?.is_working === -1) return 1
          return 2
        }
        const rankDelta = rank(a) - rank(b)
        if (rankDelta) return rankDelta
        return (a.latency_ms || 9999) - (b.latency_ms || 9999)
      })
      // 构建回退队列：直连优先，代理在后
      const sourceUrl = (u) => String(u?.url || '').trim()
      const youtubeParts = (url) => {
        const parsed = new URL(url)
        return parsed.protocol === 'youtube:'
          ? [parsed.hostname, ...parsed.pathname.split('/')].filter(Boolean)
          : parsed.pathname.split('/').filter(Boolean)
      }
      const youtubeHosts = new Set([
        'youtube.com',
        'www.youtube.com',
        'm.youtube.com',
        'youtu.be',
        'www.youtu.be',
        'youtube-nocookie.com',
        'www.youtube-nocookie.com',
      ])
      const isYoutubeHost = (host) => youtubeHosts.has(host)
      const isValidYoutubeHttpUrl = (parsed) => (
        parsed.protocol === 'https:'
          && !parsed.username
          && !parsed.password
          && (!parsed.port || parsed.port === '443')
      )
      const parseYoutubeVideoId = (url) => {
        try {
          const parsed = new URL(url)
          const host = parsed.hostname.toLowerCase()
          const parts = youtubeParts(url)
          let id = ''
          if (parsed.protocol === 'youtube:') {
            if (parts.length === 1) id = parts[0] || ''
            else if (parts.length >= 2 && ['live', 'embed', 'shorts'].includes(parts[0])) id = parts[1]
          } else if ((host === 'youtu.be' || host === 'www.youtu.be') && isValidYoutubeHttpUrl(parsed)) {
            id = parts[0] || ''
          } else if (isYoutubeHost(host) && isValidYoutubeHttpUrl(parsed) && !['youtu.be', 'www.youtu.be'].includes(host)) {
            if (parts[0]?.toLowerCase() === 'watch') id = parsed.searchParams.get('v') || ''
            if (!id && parts.length >= 2 && ['live', 'embed', 'shorts'].includes(parts[0]?.toLowerCase())) {
              id = parts[1]
            }
          }
          return /^[a-zA-Z0-9_-]{11}$/.test(id) ? id : ''
        } catch {
          return ''
        }
      }
      const parseYoutubeChannelId = (url) => {
        try {
          const parsed = new URL(url)
          const host = parsed.hostname.toLowerCase()
          const parts = youtubeParts(url)
          let id = ''
          if (parsed.protocol === 'youtube:') {
            if (/^UC[a-zA-Z0-9_-]{20,}$/.test(parts[0] || '')) id = parts[0]
            else if (parts.length >= 2 && parts[0] === 'channel') id = parts[1]
          } else if (isYoutubeHost(host) && isValidYoutubeHttpUrl(parsed) && !['youtu.be', 'www.youtu.be'].includes(host)) {
            if (parts.length >= 2 && parts[0] === 'channel') id = parts[1]
          }
          return /^UC[a-zA-Z0-9_-]{20,}$/.test(id) ? id : ''
        } catch {
          return ''
        }
      }
      const isYoutubeLiveChannelUrl = (url) => {
        try {
          const parsed = new URL(url)
          if (parsed.protocol !== 'youtube:' && !isYoutubeUrl(url)) return false
          const parts = youtubeParts(url)
          return parts[parts.length - 1] === 'live'
        } catch {
          return false
        }
      }
      const isYoutubeUrl = (url) => {
        try {
          const parsed = new URL(url)
          if (parsed.protocol === 'youtube:') return true
          const host = parsed.hostname.toLowerCase()
          return isYoutubeHost(host)
        } catch {
          return false
        }
      }
      const inferSourceType = (url) => {
        const value = String(url || '').trim().toLowerCase()
        if (parseYoutubeVideoId(url)) return 'youtube'
        if (parseYoutubeChannelId(url) && isYoutubeLiveChannelUrl(url)) return 'youtube'
        if (isYoutubeUrl(url)) return 'unsupported_youtube_url'
        if (isAdapterSchemeUrl(url)) return 'adapter'
        if (value.startsWith('rtsp://')) return 'rtsp'
        if (/\/(?:rtp|udp)\//i.test(value) || /%2f(?:rtp|udp)%2f/i.test(value)) return 'mpegts'
        if (/\.(?:ts|m2ts|mts)(?:[?#]|$)/i.test(value)) return 'mpegts'
        if (/\.flv(?:[?#]|$)/i.test(value) || /[?&]stream_type=http_flv(?:&|$)/i.test(value)) return 'http_flv'
        return 'hls'
      }
      const sourceType = (u) => {
        const inferred = inferSourceType(sourceUrl(u))
        const declared = String(u?.source_type || '').trim().toLowerCase()
        return declared && declared !== 'hls' ? declared : inferred
      }
      const proxyUrlFor = (u, options = {}) => {
        return buildChannelProxyUrl({
          apiBase: API_BASE,
          channelKey: u._canonical_key || '',
          sourceId: options.sourceId || u.source_id || '',
          expectedSourceRevision: options.expectedSourceRevision || u.source_revision || '',
          accessToken: u._access_token || '',
        })
      }
      const resolveUrlFor = (u) => {
        const key = String(u?._canonical_key || '').trim()
        const sourceId = String(u?.source_id || '').trim()
        if (!key || !sourceId) return ''
        const params = new URLSearchParams({ source_id: sourceId })
        const sourceRevision = String(u?.source_revision || '').trim()
        if (sourceRevision) params.set('expected_source_revision', sourceRevision)
        if (u._access_token) params.set('access_token', u._access_token)
        return `${API_BASE}/api/media/channel/${encodeURIComponent(key)}/resolve?${params.toString()}`
      }
      const absoluteApiUrl = (url) => {
        if (!url) return ''
        return /^https?:\/\//i.test(url) ? url : `${API_BASE}${url.startsWith('/') ? url : `/${url}`}`
      }
      const resolveAdapterSource = async (u) => {
        const resolveUrl = resolveUrlFor(u)
        if (!resolveUrl) return null
        const ctrl = new AbortController()
        activeIptvResolveControllers.add(ctrl)
        const timer = setTimeout(() => ctrl.abort(), 10_000)
        try {
          const res = await fetch(resolveUrl, { signal: ctrl.signal })
          const data = await res.json().catch(() => ({}))
          if (!res.ok || data.ok === false) {
            throw new Error(data.message || data.detail?.message || data.detail || `HTTP ${res.status}`)
          }
          const expectedSourceId = String(u?.source_id || '').trim()
          const expectedRevision = String(u?.source_revision || '').trim()
          const returnedSourceId = String(data.source_id || '').trim()
          const returnedRevision = String(data.source_revision || '').trim()
          if (
            (expectedSourceId && returnedSourceId && expectedSourceId !== returnedSourceId)
            || (expectedRevision && returnedRevision && expectedRevision !== returnedRevision)
          ) {
            const stale = new Error('播放源已更新，请重新选择')
            stale.code = 'SOURCE_REVISION_STALE'
            throw stale
          }
          return { ...data, _resolve_url: resolveUrl }
        } finally {
          clearTimeout(timer)
          activeIptvResolveControllers.delete(ctrl)
        }
      }
      const sourceForcesProxy = (u) => Boolean(
        u?.force_proxy || u?.requires_proxy_declared,
      )
      // 分两组：直连组 + 必须代理组
      const directUrls = []
      const proxyOnlyUrls = []
      const adapterSources = []
      const canonicalKey = channel.canonical_key || ''  // 聚合频道 key，用于 media API
      for (const u of sorted) {
        if (isSourceExplicitlyDisabled(u)) continue
        const url = sourceUrl(u)
        if (!url) continue
        const st = sourceType(u)
        if (st === 'youtube') {
          adapterSources.push({
            ...u,
            _canonical_key: canonicalKey,
            original_url: url,
            adapter: 'youtube',
            source_type: 'youtube',
          })
          continue
        }
        if (st === 'unsupported_youtube_url') {
          adapterSources.push({
            ...u,
            _canonical_key: canonicalKey,
            original_url: url,
            adapter: 'youtube',
            source_type: 'unsupported_youtube_url',
          })
          continue
        }
        if (st === 'adapter') {
          adapterSources.push({ ...u, _canonical_key: canonicalKey })
          continue
        }
        if (st === 'rtsp' || u.force_proxy || u.custom_ua || u.referer) {
          const proxyUrl = proxyUrlFor({ ...u, _canonical_key: canonicalKey })
          if (!proxyUrl) continue
          proxyOnlyUrls.push({
            ...u,
            _canonical_key: canonicalKey,
            url: proxyUrl,
            original_url: url,
            type: st === 'mpegts' || st === 'http_flv' ? 'direct' : 'proxy',
            via_proxy: true,
            source_type: st,
          })
        } else {
          directUrls.push({ ...u, _canonical_key: canonicalKey, url, source_type: st })
        }
      }
      const buildQueue = () => {
        const queue = []
        // 先所有直连，再所有直连的代理回退，最后是必须代理的。
        for (const u of directUrls) {
          queue.push({ ...u, url: u.url, original_url: u.original_url || u.url, type: u.type || 'direct' })
        }
        for (const u of directUrls) {
          const url = sourceUrl(u)
          const st = sourceType(u)
          if (st === 'youtube' || st === 'adapter') continue
          const proxyUrl = u.adapter_proxy_url || proxyUrlFor(u)
          if (!proxyUrl) continue
          queue.push({
            ...u,
            url: proxyUrl,
            original_url: u.original_url || url,
            type: st === 'mpegts' || st === 'http_flv' ? 'direct' : 'proxy',
            via_proxy: true,
            source_type: st,
          })
        }
        queue.push(...proxyOnlyUrls)
        return queue
      }

      const setProgressiveQueue = (queue, { initial = false, final = false } = {}) => {
        if (selectionToken !== this.iptvSelectionToken) return false
        const previous = this.iptvUrls[this.iptvUrlIndex]
        const previousKey = sourceRaceKey(previous)
        this.iptvUrls = queue
        const preservedIndex = previousKey
          ? queue.findIndex((entry) => sourceRaceKey(entry) === previousKey)
          : -1
        this.iptvUrlIndex = preservedIndex >= 0 ? preservedIndex : 0
        const channelKey = channelIdentity(channel)
        const currentKey = channelIdentity(this.currentIptvChannel)
        const currentMatches = Boolean(channelKey && currentKey && channelKey === currentKey)
          || this.currentIptvChannel === channel
        const shouldPublishCurrent = !currentMatches || this.pendingIptvChannel === channel
        if (queue.length && shouldPublishCurrent) {
          // Keep pending set until FullPlayer's pending watcher has performed
          // the hard teardown.  Clearing it in the same tick would make Vue
          // coalesce the watcher and leave the old media engine alive.
          this.currentIptvChannel = this.currentIptvChannel
            ? { ...channel }
            : channel
        } else if (!queue.length && final && shouldPublishCurrent) {
          this.currentIptvChannel = this.currentIptvChannel
            ? { ...channel }
            : channel
        }
        if (this.pendingIptvChannel && selectionToken === this.iptvSelectionToken) {
          // Give FullPlayer's pending watcher one scheduler turn to perform
          // teardown, then release the UI-only pending marker even when the
          // store is used without the component mounted (tests/background).
          Promise.resolve().then(() => {
            if (
              selectionToken === this.iptvSelectionToken
              && this.pendingIptvChannel
              && channelIdentity(this.pendingIptvChannel) === channelIdentity(channel)
            ) {
              this.pendingIptvChannel = null
            }
          })
        }
        if (initial) {
          this.playbackError = queue.length ? '' : '正在获取可播放源'
          this.isLoading = true
          this.isPlaying = false
        } else if (queue.length && !this.playbackError) {
          this.isLoading = true
        }
        return true
      }

      const addPendingAdapterProxy = (u) => {
        const fallbackProxyUrl = proxyUrlFor(u)
        if (!fallbackProxyUrl) return
        if (proxyOnlyUrls.some((entry) => entry.source_id && entry.source_id === u.source_id && entry.adapter_transport_pending)) return
        proxyOnlyUrls.push({
          ...u,
          url: fallbackProxyUrl,
          original_url: u.original_url || sourceUrl(u),
          adapter: u.adapter || adapterNameFromUrl(sourceUrl(u)),
          type: 'proxy',
          via_proxy: true,
          source_type: 'adapter',
          adapter_transport_pending: true,
        })
      }

      const applyResolvedAdapter = (u, resolved) => {
        const url = sourceUrl(u)
        const adapter = u.adapter || adapterNameFromUrl(url)
        const originalUrl = u.original_url || url
        const fallbackProxyUrl = proxyUrlFor(u)
        const resolvedUrl = String(resolved?.url || '').trim()
        const proxyUrl = absoluteApiUrl(resolved?.proxy_url) || fallbackProxyUrl
        const adapterAllowsDirect = !resolved?.requires_proxy && resolved?.direct_playable !== false
        const canDirectPlay = Boolean(resolvedUrl && adapterAllowsDirect && !sourceForcesProxy(u))
        const sourceId = String(u.source_id || '').trim()
        const pendingEntries = proxyOnlyUrls.filter((entry) => (
          sourceId
          && entry?.source_id === sourceId
          && entry?.adapter_transport_pending
        ))
        const activeEntry = this.iptvUrls[this.iptvUrlIndex]
        const activePending = pendingEntries.some((entry) => sourceRaceKey(entry) === sourceRaceKey(activeEntry))
        if (!canDirectPlay && proxyUrl && activePending) {
          const pendingEntry = pendingEntries[0]
          Object.assign(pendingEntry, {
            url: proxyUrl,
            original_url: originalUrl,
            adapter,
            type: 'proxy',
            via_proxy: true,
            source_type: resolved?.source_type === 'probe_only' ? 'hls' : resolved?.source_type || 'hls',
            adapter_transport_pending: false,
          })
          return
        }
        for (let index = proxyOnlyUrls.length - 1; index >= 0; index -= 1) {
          if (
            sourceId
            && proxyOnlyUrls[index]?.source_id === sourceId
            && proxyOnlyUrls[index]?.adapter_transport_pending
            && (!activePending || !canDirectPlay)
          ) {
            proxyOnlyUrls.splice(index, 1)
          }
        }
        if (canDirectPlay) {
          directUrls.push({
            ...u,
            url: resolvedUrl,
            original_url: originalUrl,
            adapter,
            adapter_source_url: resolved._resolve_url,
            adapter_proxy_url: proxyUrl,
            adapter_volatile_url: resolved.volatile_url === true,
            source_type: resolved.source_type || 'hls',
            type: 'direct',
          })
          return
        }
        if (proxyUrl) {
          proxyOnlyUrls.push({
            ...u,
            url: proxyUrl,
            original_url: originalUrl,
            adapter,
            type: 'proxy',
            via_proxy: true,
            source_type: resolved?.source_type === 'probe_only' ? 'hls' : resolved?.source_type || 'hls',
          })
        }
      }

      const handleAdapterResolveFailure = (u, error) => {
        const url = sourceUrl(u)
        const adapter = u.adapter || adapterNameFromUrl(url)
        const originalUrl = u.original_url || url
        const fallbackProxyUrl = proxyUrlFor(u)
        if (selectionToken !== this.iptvSelectionToken) return
        if (error?.code === 'SOURCE_REVISION_STALE') return
        if (error?.name !== 'AbortError') console.warn('[IPTV] adapter resolve failed:', error?.message || error)
        if (!sourceForcesProxy(u)) {
          directUrls.push({
            ...u,
            url,
            original_url: originalUrl,
            adapter,
            adapter_source_url: resolveUrlFor(u),
            adapter_volatile_url: true,
            source_type: 'adapter',
            type: 'direct',
          })
        }
        addPendingAdapterProxy(u)
      }

      const addYoutubeSource = (u) => {
        const originalUrl = u.original_url || sourceUrl(u)
        const youtubeVideoId = parseYoutubeVideoId(originalUrl) || u.youtube_video_id || ''
        const youtubeChannelId = parseYoutubeChannelId(originalUrl) || u.youtube_channel_id || ''
        if ((youtubeVideoId || youtubeChannelId) && !sourceForcesProxy(u)) {
          directUrls.push({
            ...u,
            url: originalUrl,
            original_url: originalUrl,
            type: 'youtube',
            engine: 'youtube',
            source_type: 'youtube',
            youtube_video_id: youtubeVideoId,
            youtube_channel_id: youtubeChannelId,
            youtube_live_embed_url: youtubeChannelId && !youtubeVideoId
              ? `https://www.youtube.com/embed/live_stream?channel=${encodeURIComponent(youtubeChannelId)}&autoplay=1&playsinline=1&controls=1&rel=0`
              : '',
          })
        }
      }

      const progressive = options.progressive === true
      if (progressive) {
        // Publish anything already known before waiting for provider resolves.
        // Adapter-only channels wait for the first completed provider resolve;
        // their fallback entry is added only when that provider fails. This
        // avoids starting an opaque proxy before a real transport is known.
        for (const u of adapterSources) {
          const adapter = u.adapter || adapterNameFromUrl(sourceUrl(u))
          if (adapter === 'youtube') addYoutubeSource(u)
        }
        setProgressiveQueue(buildQueue(), { initial: true })

        const pendingAdapters = adapterSources.filter((u) => (u.adapter || adapterNameFromUrl(sourceUrl(u))) !== 'youtube')
        let nextAdapterIndex = 0
        const worker = async () => {
          while (selectionToken === this.iptvSelectionToken) {
            const u = pendingAdapters[nextAdapterIndex++]
            if (!u) return
            try {
              const resolved = await resolveAdapterSource(u)
              if (selectionToken !== this.iptvSelectionToken) return
              if (!resolved) throw new Error('adapter resolve unavailable')
              applyResolvedAdapter(u, resolved)
              setProgressiveQueue(buildQueue())
            } catch (error) {
              if (selectionToken !== this.iptvSelectionToken) return
              handleAdapterResolveFailure(u, error)
              setProgressiveQueue(buildQueue())
            }
          }
        }
        const workerCount = Math.min(3, pendingAdapters.length)
        if (workerCount) {
          void Promise.all(Array.from({ length: workerCount }, () => worker()))
            .then(() => {
              if (selectionToken !== this.iptvSelectionToken) return
              setProgressiveQueue(buildQueue(), { final: true })
              if (!this.iptvUrls.length) {
                this.playbackError = '没有可播放的源'
                this.isLoading = false
              }
            })
            .catch((error) => {
              if (selectionToken !== this.iptvSelectionToken) return
              this.playbackError = error?.message || '频道起播失败'
              this.isLoading = false
            })
        } else if (!this.iptvUrls.length) {
          setProgressiveQueue(buildQueue(), { final: true })
          this.playbackError = this.iptvUrls.length ? '' : '没有可播放的源'
          this.isLoading = Boolean(this.iptvUrls.length)
        }
        return
      }

      for (const u of adapterSources) {
        const url = sourceUrl(u)
        const adapter = u.adapter || adapterNameFromUrl(url)
        const originalUrl = u.original_url || url
        const fallbackProxyUrl = proxyUrlFor(u)
        if (adapter === 'youtube') {
          addYoutubeSource(u)
        }
        if (adapter !== 'youtube') {
          try {
            const resolved = await resolveAdapterSource(u)
            if (selectionToken !== this.iptvSelectionToken) return
            const resolvedUrl = String(resolved?.url || '').trim()
            const proxyUrl = absoluteApiUrl(resolved?.proxy_url) || fallbackProxyUrl
            // Adapter 自身声明 requires_proxy/direct_playable=false 是硬约束；
            // Market/订阅侧 force_proxy 只能额外要求代理，不能覆盖 adapter 硬约束为 direct。
            const adapterAllowsDirect = !resolved?.requires_proxy && resolved?.direct_playable !== false
            const canDirectPlay = Boolean(resolvedUrl && adapterAllowsDirect && !sourceForcesProxy(u))
            if (canDirectPlay) {
              directUrls.push({
                ...u,
                url: resolvedUrl,
                original_url: originalUrl,
                adapter,
                adapter_source_url: resolved._resolve_url,
                adapter_proxy_url: proxyUrl,
                adapter_volatile_url: resolved.volatile_url === true,
                source_type: resolved.source_type || 'hls',
                type: 'direct',
              })
            } else if (proxyUrl) {
              proxyOnlyUrls.push({
                ...u,
                url: proxyUrl,
                original_url: originalUrl,
                adapter,
                type: 'proxy',
                via_proxy: true,
                source_type: resolved?.source_type === 'probe_only' ? 'hls' : resolved?.source_type || 'hls',
              })
            }
            continue
          } catch (e) {
            if (selectionToken !== this.iptvSelectionToken) return
            if (e?.code === 'SOURCE_REVISION_STALE') continue
            console.warn('[IPTV] adapter resolve failed:', e?.message || e)
            // 非强制代理：resolve 失败只是本次未取到流地址，不得把 source 改写为 proxy-only，
            // 也不得从菜单删除。保留原始 adapter identity 作为直连 entry（FullPlayer 的
            // volatile adapter re-resolve 路径会在实际播放时重新调用 /resolve）。
            if (!sourceForcesProxy(u)) {
              const resolveUrl = resolveUrlFor(u)
              directUrls.push({
                ...u,
                url,
                original_url: originalUrl,
                adapter,
                adapter_source_url: resolveUrl,
                adapter_volatile_url: true,
                source_type: 'adapter',
                type: 'direct',
              })
              if (fallbackProxyUrl) {
                proxyOnlyUrls.push({
                  ...u,
                  url: fallbackProxyUrl,
                  original_url: originalUrl,
                  adapter,
                  type: 'proxy',
                  via_proxy: true,
                  source_type: 'adapter',
                  adapter_transport_pending: true,
                })
              }
              continue
            }
            // force_proxy：resolve 失败时保留 channel proxy fallback
          }
        }
        if (fallbackProxyUrl) {
          proxyOnlyUrls.push({
            ...u,
            url: fallbackProxyUrl,
            original_url: originalUrl,
            adapter,
            type: 'proxy',
            via_proxy: true,
            source_type: adapter === 'youtube' ? 'hls' : 'adapter',
            adapter_transport_pending: adapter !== 'youtube',
          })
        }
      }
      if (selectionToken !== this.iptvSelectionToken) return
      const list = buildQueue()
      this.currentIptvChannel = channel
      this.pendingIptvChannel = null
      this.iptvUrls = list
      this.iptvUrlIndex = 0
      this.playbackError = list.length ? '' : '没有可播放的源'
      this.isLoading = Boolean(list.length)
      this.isPlaying = false
      // isPlaying 由实际播放事件设置，不提前设
      } catch (e) {
        if (selectionToken !== this.iptvSelectionToken) return
        this.pendingIptvChannel = null
        throw e
      }
    },

    iptvFallbackNext() {
      if (this.iptvUrlIndex < this.iptvUrls.length - 1) {
        this.iptvUrlIndex++
        this.playbackError = `源不可用，正在切换备用源 (${this.iptvUrlIndex + 1}/${this.iptvUrls.length})`
        this.isLoading = true
        return true
      }
      this.playbackError = '所有播放源均不可用'
      this.isPlaying = false
      this.isLoading = false
      return false
    },

    async reResolveAdapterUrl(resolveUrl) {
      const url = String(resolveUrl || '').trim()
      if (!url || !/\/api\/media\/channel\/.+\/resolve(?:\?|$)/i.test(url)) return null
      const ctrl = new AbortController()
      activeIptvResolveControllers.add(ctrl)
      const timer = setTimeout(() => ctrl.abort(), 10_000)
      try {
        const res = await fetch(url, { signal: ctrl.signal, cache: 'no-store' })
        const data = await res.json().catch(() => ({}))
        if (!res.ok || data.ok === false) {
          throw new Error(data.message || data.detail?.message || data.detail || `HTTP ${res.status}`)
        }
        const parsed = new URL(url, API_BASE || window.location.origin)
        const expectedSourceId = parsed.searchParams.get('source_id') || ''
        const expectedRevision = parsed.searchParams.get('expected_source_revision') || ''
        if (
          (expectedSourceId && data.source_id && expectedSourceId !== String(data.source_id))
          || (expectedRevision && data.source_revision && expectedRevision !== String(data.source_revision))
        ) {
          const stale = new Error('播放源已更新，请重新选择')
          stale.code = 'SOURCE_REVISION_STALE'
          throw stale
        }
        return data
      } finally {
        clearTimeout(timer)
        activeIptvResolveControllers.delete(ctrl)
      }
    },
  },
})
