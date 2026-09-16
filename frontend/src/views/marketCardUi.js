const BADGE_TONE_CLASSES = Object.freeze({
  neutral: 'market-region-neutral',
  rose: 'market-region-rose',
  sky: 'market-region-sky',
  emerald: 'market-region-emerald',
  orange: 'market-region-orange',
  violet: 'market-region-violet',
})

// This registry maps an explicit package display identity to an existing visual.
// It never inspects package names, regions, operators, or plugin manifests.
const BUILTIN_BRANDS = Object.freeze({
  waveflow: { text: 'WF', toneClass: 'market-region-violet' },
  youtube: { text: 'YT', toneClass: 'market-region-rose' },
  'china-mobile': { text: '移', toneClass: 'market-region-sky' },
  'china-unicom': { text: '联', toneClass: 'market-region-rose' },
  'china-telecom': { text: '电', toneClass: 'market-region-orange' },
  'china-broadcast': { text: '广', toneClass: 'market-region-emerald' },
})

function text(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function validBadgeTone(value) {
  return typeof value === 'string' && BADGE_TONE_CLASSES[value] ? value : 'neutral'
}

export function packageCardTitle(pkg) {
  return text(pkg?.name)
}

export function packageSubtitle(pkg) {
  return text(pkg?.display?.subtitle)
}

export function packageSummary(pkg) {
  return text(pkg?.display?.summary)
}

export function packageIdentity(pkg) {
  const display = pkg?.display && typeof pkg.display === 'object' ? pkg.display : {}
  const identity = display.identity && typeof display.identity === 'object' ? display.identity : {}
  const icon = identity.icon && typeof identity.icon === 'object' ? identity.icon : {}
  const badge = display.badge && typeof display.badge === 'object' ? display.badge : {}
  const brand = (icon.type === 'builtin' && BUILTIN_BRANDS[text(icon.name).toLowerCase()])
    || BUILTIN_BRANDS[text(identity.brand).toLowerCase()]
  const explicitText = text(badge.text)
  const tone = validBadgeTone(badge.tone)

  return {
    text: explicitText || brand?.text || '',
    toneClass: explicitText
      ? BADGE_TONE_CLASSES[tone]
      : brand?.toneClass || 'market-region-neutral',
    imageUrl: icon.type === 'image' ? text(icon.url) : '',
    iconName: icon.type === 'builtin' ? text(icon.name) : '',
  }
}

export function packageScaleLabel(pkg) {
  if (!pkg) return ''
  if (pkg.kind === 'logo_pack') {
    const logoCount = Number(pkg.logo_count)
    if (Number.isFinite(logoCount) && logoCount > 0) return `${logoCount} 个台标`
    return ''
  }
  const channelCount = Number(pkg.channel_count)
  if (Number.isFinite(channelCount) && channelCount > 0) return `${channelCount} 个频道`
  return ''
}

export function packageTagItems(pkg) {
  const values = Array.isArray(pkg?.tags) ? pkg.tags : []
  return values.map((value, index) => ({
    key: `${index}:${text(value)}`,
    label: text(value),
    accentClass: 'market-tag-neutral',
  })).filter((tag) => tag.label)
}
