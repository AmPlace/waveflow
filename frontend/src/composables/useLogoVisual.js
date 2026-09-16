import { ref } from 'vue'

export function classifyLogoMode(width, height, { enableWide = false } = {}) {
  const naturalWidth = Number(width)
  const naturalHeight = Number(height)
  if (!naturalWidth || !naturalHeight) return 'badge'

  const ratio = naturalWidth / naturalHeight
  const isLargeCover = naturalWidth >= 480
    && naturalHeight >= 240
    && ratio >= 1.55
    && ratio <= 1.9
  if (isLargeCover) return 'cover'

  // Wide marks must remain contain-rendered. A ratio-only check is deliberately
  // conservative so ordinary 2:1 badge assets (for example CCTV marks) stay
  // in the badge path.
  const isWide = enableWide
    && naturalWidth >= 96
    && naturalHeight >= 24
    && ratio >= 2.2
  return isWide ? 'wide' : 'badge'
}

export function useLogoVisual(options = {}) {
  const failedLogoKeys = ref({})
  const logoVisualModes = ref({})

  function logoUrl(item) {
    return String(options.getLogoUrl?.(item) || '').trim()
  }

  function displayName(item) {
    const value = String(options.getDisplayName?.(item) || '').trim()
    return value || options.fallbackName || '未知频道'
  }

  function identityKey(item) {
    return String(options.getIdentityKey?.(item) || displayName(item))
  }

  function logoFailureKey(item) {
    const key = String(options.getFailureKey?.(item) || '').trim()
    if (key) return key
    return `${identityKey(item)}|${logoUrl(item)}`
  }

  function logoVisualKey(item) {
    const key = String(options.getVisualKey?.(item) || '').trim()
    if (key) return key
    return logoFailureKey(item)
  }

  function shouldShowLogo(item) {
    // Visual metadata is loaded by the Home IntersectionObserver,
    // 不在渲染路径中触发网络请求
    const url = logoUrl(item)
    if (!url) return false
    return !failedLogoKeys.value[logoFailureKey(item)]
  }

  function logoVisualMode(item) {
    return logoVisualModes.value[logoVisualKey(item)] || 'badge'
  }

  function logoStageClass(item) {
    return `channel-card__logo-stage--${logoVisualMode(item)}`
  }

  function logoImageClass(item) {
    return `channel-card__center-logo--${logoVisualMode(item)}`
  }

  function classifyLogo(item, event) {
    const img = event?.target
    if (!img) return
    const width = Number(img.naturalWidth || 0)
    const height = Number(img.naturalHeight || 0)
    if (!width || !height) return

    if (options.onBeforeClassify?.(item, { width, height, event })) return

    const mode = classifyLogoMode(width, height, {
      enableWide: options.enableWide === true,
    })
    const key = logoVisualKey(item)
    if (logoVisualModes.value[key] === mode) return
    logoVisualModes.value = {
      ...logoVisualModes.value,
      [key]: mode,
    }
  }

  function markLogoFailed(item) {
    if (options.onBeforeFail?.(item)) return
    failedLogoKeys.value = {
      ...failedLogoKeys.value,
      [logoFailureKey(item)]: true,
    }
  }

  function textLogoSizeClass(item) {
    const length = Array.from(displayName(item)).length
    if (length <= 4) return 'channel-card__text-logo--xl'
    if (length <= 8) return 'channel-card__text-logo--lg'
    if (length <= 14) return 'channel-card__text-logo--md'
    return 'channel-card__text-logo--sm'
  }

  return {
    failedLogoKeys,
    logoVisualModes,
    displayName,
    shouldShowLogo,
    logoVisualMode,
    logoStageClass,
    logoImageClass,
    classifyLogo,
    markLogoFailed,
    textLogoSizeClass,
  }
}
