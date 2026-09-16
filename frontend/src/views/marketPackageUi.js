import { contractLabel, runtimeLabel } from './pluginPresentation.js'
export { permissionLabel } from './pluginPresentation.js'

export const CONTENT_PACKAGE = 'content_package'
export const PLUGIN_PACKAGE = 'plugin_package'
export const LOGO_PACKAGE_KIND = 'logo_pack'

export function isPluginPackage(pkg) {
  return pkg?.package_type === PLUGIN_PACKAGE
}

export function isLogoPackage(pkg) {
  return !isPluginPackage(pkg) && pkg?.kind === LOGO_PACKAGE_KIND &&
    Array.isArray(pkg?.content_capabilities) && pkg.content_capabilities.includes('logos')
}

export function pluginIdentity(pkg) {
  const plugin = pkg?.plugin || pkg?.plugin_manifest || {}
  const publisher = String(plugin.publisher_id || '')
  const id = String(plugin.plugin_id || '')
  return publisher && id ? `${publisher}/${id}` : ''
}

export function packageInstallable(pkg) {
  return isPluginPackage(pkg) ? Boolean(pkg?.plugin_installable) : Boolean(pkg?.supported_in_v1 && pkg?.importable)
}

export function packageActionLabel(pkg, action) {
  if (isLogoPackage(pkg)) return ({ install: '安装 Logo', installing: '安装中…', update: '更新 Logo', updating: '更新中…', uninstall: '卸载 Logo' })[action] || action
  if (!isPluginPackage(pkg)) return ({ install: '导入', installing: '导入中…', update: '更新', updating: '更新中…', uninstall: '卸载' })[action] || action
  return ({ install: '安装 Plugin', installing: '安装中…', update: '更新 Plugin', updating: '更新中…', uninstall: '卸载 Plugin' })[action] || action
}

export function providerContractLabels(pkg) {
  const values = pkg?.plugin?.provider_contracts || pkg?.plugin_manifest?.provider_contracts || []
  return values.map((item) => {
    const contract = typeof item === 'string' ? item : item?.contract
    return contractLabel(contract)
  }).filter(Boolean)
}

export function pluginSchemeLabels(pkg) {
  const values = pkg?.plugin?.owned_schemes || pkg?.plugin_manifest?.owned_schemes || []
  return values.map((item) => typeof item === 'string' ? item : item?.scheme)
    .filter(Boolean)
    .map(String)
}

export function requestedPermissions(pkg) {
  if (Array.isArray(pkg?.plugin?.permissions)) return pkg.plugin.permissions.map(String)
  const raw = pkg?.plugin_manifest?.permissions || {}
  const result = []
  if (raw.network?.managed === true) result.push('network.managed')
  if (raw.network?.direct === true) result.push('network.direct')
  if (raw.network?.allow_http === true) result.push('network.managed_http')
  return result.concat(Object.keys(raw).filter(key => key !== 'network' && raw[key] === true))
}

export function pluginRuntimeLabel(pkg) {
  const runtime = pkg?.plugin_manifest?.runtime || {}
  if (runtime.type) return runtimeLabel(runtime)
  const platforms = pkg?.plugin?.platforms || []
  const kinds = new Set(platforms.map((item) => item.runtime).filter(Boolean))
  return [...kinds].map(type => runtimeLabel({ type })).join(' / ') || '未提供运行环境信息'
}

export function pluginDependencies(pkg) {
  const lock = pkg?.plugin_manifest?.runtime?.dependency_lock || {}
  const items = Array.isArray(lock.artifacts) ? lock.artifacts : (pkg?.plugin?.dependencies || [])
  const seen = new Set()
  return items.filter((item) => {
    const key = `${item?.name || ''}@${item?.version || ''}`
    if (!item?.name || seen.has(key)) return false
    seen.add(key)
    return true
  }).map(item => ({ name: String(item.name), version: String(item.version || '') }))
}

export function pluginPublisher(pkg) {
  return pkg?.publisher?.name || pkg?.publisher?.id || pkg?.plugin?.publisher_id || pkg?.plugin_manifest?.publisher_id || ''
}
