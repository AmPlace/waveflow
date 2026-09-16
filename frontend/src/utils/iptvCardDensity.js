export const IPTV_CARD_DENSITY_STORAGE_KEY = 'waveflow.iptv.card-density'

export const IPTV_CARD_DENSITIES = Object.freeze({
  STANDARD: 'standard',
  COMPACT: 'compact',
})

export function normalizeIptvCardDensity(value) {
  return value === IPTV_CARD_DENSITIES.COMPACT || value === IPTV_CARD_DENSITIES.STANDARD
    ? value
    : ''
}

export function defaultIptvCardDensity(viewportWidth) {
  return Number(viewportWidth) < 1024
    ? IPTV_CARD_DENSITIES.COMPACT
    : IPTV_CARD_DENSITIES.STANDARD
}

function defaultStorage() {
  if (typeof window !== 'undefined') return window.localStorage
  return globalThis?.localStorage
}

export function readIptvCardDensityPreference(storage = defaultStorage()) {
  try {
    return normalizeIptvCardDensity(storage?.getItem(IPTV_CARD_DENSITY_STORAGE_KEY))
  } catch {
    return ''
  }
}

export function writeIptvCardDensityPreference(value, storage = defaultStorage()) {
  const density = normalizeIptvCardDensity(value)
  if (!density) return false
  try {
    storage?.setItem(IPTV_CARD_DENSITY_STORAGE_KEY, density)
    return true
  } catch {
    return false
  }
}
