import { API_BASE, isDesktop } from '../apiBase.js'

function mapRadioStation(station) {
  const stationId = String(station?.station_id || '').trim()
  const sources = Array.isArray(station?.sources)
    ? station.sources.filter((source) => source && source.source_id && source.lifecycle_state !== 'expired')
    : []
  if (!stationId || sources.length === 0) return null

  const source = sources[0]
  const safeSources = sources.map((item) => ({
    source_id: String(item.source_id),
    owner_identity: String(item.owner_identity || ''),
    provider_key: String(item.provider_key || ''),
    provider_station_id: String(item.provider_station_id || ''),
    source_discriminator: String(item.source_discriminator || ''),
    source_revision: String(item.source_revision || ''),
    explicit_priority: item.explicit_priority,
    health_status: String(item.health_status || ''),
    lifecycle_state: String(item.lifecycle_state || ''),
    catalog_expires_at: item.catalog_expires_at,
    resolve_expires_at: item.resolve_expires_at,
  }))
  const metadata = station?.metadata && typeof station.metadata === 'object' ? station.metadata : {}
  const metadataTags = Array.isArray(metadata.tags)
    ? metadata.tags.map((tag) => String(tag || '').trim()).filter(Boolean)
    : []
  const name = String(station?.name || station?.provider_station_id || stationId).trim()
  return {
    id: stationId,
    name,
    subtitle: String(metadata.subtitle || station?.frequency || '').trim(),
    logoUrl: String(station?.logo_url || '').trim(),
    logoText: name.slice(0, 1) || '?',
    // New Radio playback is selected by persisted source_id.  It never
    // carries or accepts an upstream URL in the frontend state.
    radioStationId: stationId,
    radioSourceId: String(source.source_id),
    radioSources: safeSources,
    radioDomain: 'radio',
    catalogStatus: String(station?.catalog_status || '').trim(),
    radioRegion: String(station?.country || '').trim(),
    radioType: typeof metadata.tag === 'string' ? metadata.tag.trim() : '',
    livePath: true,
    directPlay: false,
    tags: [
      station?.country,
      station?.group_name,
      station?.language,
      ...metadataTags,
      typeof metadata.tag === 'string' ? metadata.tag : '',
    ]
      .map((tag) => String(tag || '').trim())
      .filter(Boolean),
  }
}

function normalizeCatalogStates(states) {
  return (Array.isArray(states) ? states : [])
    .filter((state) => state && typeof state === 'object')
    .map((state) => ({
      owner_identity: String(state.owner_identity || '').trim(),
      status: String(state.status || '').trim().toLowerCase(),
      station_count: Number.isFinite(Number(state.station_count))
        ? Number(state.station_count)
        : 0,
    }))
    .filter((state) => state.status)
}

export function summarizeRadioCatalogState(stations, catalogStates = []) {
  const rows = Array.isArray(stations) ? stations : []
  const states = normalizeCatalogStates(catalogStates)
  const rowStatuses = rows
    .map((station) => String(station?.catalogStatus || '').trim().toLowerCase())
    .filter(Boolean)
  const statuses = [...states.map((state) => state.status), ...rowStatuses]

  if (statuses.includes('stale')) return 'stale'
  if (statuses.includes('degraded')) return 'degraded'
  if (statuses.includes('failed') || statuses.includes('expired')) {
    return rows.length > 0 ? 'degraded' : 'error'
  }
  if (rows.length === 0) return 'empty'
  return 'success'
}

async function fetchCatalogData(endpoint, { fetchImpl = fetch, signal } = {}) {
  const controller = new AbortController()
  let timer = null
  let timedOut = false
  let removeAbortListener = null
  if (signal) {
    if (signal.aborted) return { status: 'cancelled' }
    const abort = () => controller.abort()
    signal.addEventListener('abort', abort, { once: true })
    removeAbortListener = () => signal.removeEventListener('abort', abort)
  }

  try {
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, 15_000)
    const response = await fetchImpl(`${API_BASE}${endpoint}`, {
      signal: controller.signal,
      credentials: isDesktop ? 'include' : 'same-origin',
    })
    if (!response.ok) return { status: 'error', errorKind: 'http' }
    const body = await response.json()
    return { status: 'success', body }
  } catch (error) {
    if (error?.name === 'AbortError') {
      return timedOut
        ? { status: 'error', errorKind: 'timeout' }
        : { status: 'cancelled' }
    }
    return { status: 'error', errorKind: 'network' }
  } finally {
    if (timer) clearTimeout(timer)
    removeAbortListener?.()
  }
}

export async function fetchRadioCatalog(options = {}) {
  const result = await fetchCatalogData('/api/radio/stations', options)
  if (result.status !== 'success') return result
  const rows = Array.isArray(result.body) ? result.body : result.body?.stations
  if (!Array.isArray(rows)) return { status: 'error', errorKind: 'invalid_response' }
  return {
    status: 'success', stations: rows.map(mapRadioStation).filter(Boolean),
    catalogStates: normalizeCatalogStates(result.body?.catalog_states),
  }
}

export async function fetchRadioProgramme(station, { fetchImpl = fetch } = {}) {
  const stationId = String(station?.radioStationId || '').trim()
  const sourceId = String(station?.radioSourceId || '').trim()
  if (!stationId || !sourceId) return null
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 10_000)
  try {
    const query = new URLSearchParams({ source_id: sourceId })
    const response = await fetchImpl(
      `${API_BASE}/api/radio/stations/${encodeURIComponent(stationId)}/programme?${query}`,
      { signal: controller.signal, credentials: isDesktop ? 'include' : 'same-origin' },
    )
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

export function mapRadioStationForTest(station) {
  return mapRadioStation(station)
}
