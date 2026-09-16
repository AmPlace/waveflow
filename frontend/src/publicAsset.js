export function publicAsset(path) {
  const value = String(path || '')
  if (!value) return ''
  if (/^(https?:|data:|blob:|file:)/i.test(value)) return value
  return `${import.meta.env.BASE_URL}${value.replace(/^\/+/, '')}`
}
