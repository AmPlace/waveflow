// Generic source visual fallback order. Provider identity is deliberately
// not inspected here: stable channel art and avatar are safe offline
// fallbacks, while dynamic live art is only eligible for an active stream.
export function channelVisualCandidates(logoUrl, visual = {}) {
  const candidates = []
  const add = (value) => {
    const url = String(value || '').trim()
    if (url) candidates.push(url)
  }

  add(logoUrl)
  add(visual?.stable_cover_url)
  add(visual?.avatar_url)
  // Existing content thumbnails retain their generic content role. A
  // live/dynamic cover is state-gated so an offline screenshot cannot become
  // the channel-wall primary visual.
  if (visual?.cover_role === 'content') add(visual.cover_url)
  if (visual?.is_live === true) add(visual?.dynamic_cover_url || visual?.cover_url)

  return Array.from(new Set(candidates))
}
