const FALSE_VALUES = new Set(['', '0', 'false', 'no', 'off'])
const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on'])

export function normalizeMarketSourceBoolean(value) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) && value !== 0
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (FALSE_VALUES.has(normalized)) return false
    if (TRUE_VALUES.has(normalized)) return true
  }
  return Boolean(value)
}

export function marketSourceDraft(source) {
  return {
    ...source,
    enabled: normalizeMarketSourceBoolean(source?.enabled),
    allow_private: normalizeMarketSourceBoolean(source?.allow_private),
    replace_url: false,
    replacement_url: '',
    persisted: {
      name: source?.name || '',
      enabled: normalizeMarketSourceBoolean(source?.enabled),
      allow_private: normalizeMarketSourceBoolean(source?.allow_private),
    },
  }
}

export function projectMarketSources(sources) {
  return (sources || []).map(marketSourceDraft)
}

export function createLatestMarketSourceProjection(apply) {
  let revision = 0

  return {
    begin() {
      return ++revision
    },
    publish(requestRevision, sources) {
      if (requestRevision !== revision) return false
      apply(projectMarketSources(sources))
      return true
    },
  }
}
