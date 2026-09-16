export const EPG_BATCH_REFRESH_GRACE_MS = 1_200
export const EPG_BATCH_REFRESH_MIN_MS = 1_000
export const EPG_BATCH_REFRESH_FALLBACK_MS = 15 * 60 * 1_000
export const EPG_BATCH_REFRESH_MAX_MS = 6 * 60 * 60 * 1_000

export function formatEpgClock(value) {
  const raw = String(value ?? '').trim()
  if (!raw) return ''
  const date = new Date(raw)
  if (!Number.isFinite(date.getTime())) return ''
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

export function channelProgrammeSubtitle(program, fallback = '') {
  const title = String(program?.title || '').trim()
  if (!title) return String(fallback || '')
  const end = formatEpgClock(program?.stop)
  return end ? `${title} · ${end} 结束` : title
}

export function epgBatchRefreshDelay(epgMap, now = Date.now()) {
  const boundaries = []
  for (const entry of Object.values(epgMap || {})) {
    const currentStop = new Date(entry?.current?.stop).getTime()
    if (Number.isFinite(currentStop) && currentStop > now) boundaries.push(currentStop)

    if (!entry?.current) {
      const nextStart = new Date(entry?.next?.start).getTime()
      if (Number.isFinite(nextStart) && nextStart > now) boundaries.push(nextStart)
    }
  }
  if (!boundaries.length) return EPG_BATCH_REFRESH_FALLBACK_MS
  const delay = Math.min(...boundaries) - now + EPG_BATCH_REFRESH_GRACE_MS
  return Math.max(EPG_BATCH_REFRESH_MIN_MS, Math.min(delay, EPG_BATCH_REFRESH_MAX_MS))
}
