export function normalizeRadioTimestamp(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    return value < 100000000000 ? value * 1000 : value
  }
  if (typeof value !== 'string') return Number.NaN
  const text = value.trim()
  if (!text) return Number.NaN
  if (/^\d+(?:\.\d+)?$/.test(text)) {
    const seconds = Number(text)
    return Number.isFinite(seconds) ? (seconds < 100000000000 ? seconds * 1000 : seconds) : Number.NaN
  }
  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? parsed : Number.NaN
}

export function currentRadioProgramme(items, now = Date.now()) {
  const list = Array.isArray(items) ? items : []
  const current = list.find((item) => {
    const start = normalizeRadioTimestamp(item?.start)
    const end = normalizeRadioTimestamp(item?.end)
    return Number.isFinite(start) && Number.isFinite(end) && start <= now && now < end
  })
  if (current) return current

  // A snapshot with no time-bounded entries represents a provider's
  // current-only label.  A timed schedule with no current item must not fall
  // back to its first future programme.
  const hasTimedEntry = list.some((item) => item?.start != null || item?.end != null)
  return hasTimedEntry ? null : list.find((item) => item?.subtitle || item?.title) || null
}
