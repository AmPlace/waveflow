const desktopApiBase = typeof window !== 'undefined'
  ? window.WAVEFLOW_DESKTOP?.apiBase
  : ''

const currentOrigin = typeof window !== 'undefined' && /^https?:$/.test(window.location.protocol)
  ? window.location.origin
  : ''

const viteEnv = import.meta.env || {}

export const isDesktop = Boolean(desktopApiBase)
export const API_BASE = desktopApiBase || viteEnv.VITE_API_BASE_URL || currentOrigin || ''
