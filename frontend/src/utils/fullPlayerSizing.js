export const FULL_PLAYER_ASPECT_RATIO = 16 / 9
export const FULL_PLAYER_DESKTOP_MIN_GAP = 20
export const FULL_PLAYER_DESKTOP_MAX_GAP = 24
export const FULL_PLAYER_MOBILE_MAX_WIDTH = 980
export const FULL_PLAYER_SMALL_DESKTOP_RAIL_MIN = 260
export const FULL_PLAYER_SMALL_DESKTOP_RAIL_MAX = 300
export const FULL_PLAYER_MEDIUM_RAIL_MIN = 300
export const FULL_PLAYER_MEDIUM_RAIL_MAX = 340
export const FULL_PLAYER_WIDE_RAIL_MIN = 350
export const FULL_PLAYER_WIDE_RAIL_MAX = 400
export const FULL_PLAYER_WIDE_AVAILABLE_WIDTH = 1680
export const FULL_PLAYER_STAGE_ASPECT_MIN = 1.55
export const FULL_PLAYER_STAGE_ASPECT_MAX = 1.9
export const FULL_PLAYER_RAIL_LETTERBOX_RATIO = 0.26
export const FULL_PLAYER_RAIL_LETTERBOX_MIN = 96
export const FULL_PLAYER_RAIL_LETTERBOX_MAX = 220
export const FULL_PLAYER_RAIL_VIDEO_MIN_WIDTH = 900
export const FULL_PLAYER_RAIL_VIDEO_TARGET_WIDTH = 1080
export const FULL_PLAYER_RAIL_VIDEO_RATIO = 0.78

function finiteOr(value, fallback) {
  return Number.isFinite(Number(value)) ? Number(value) : fallback
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

export function fullPlayerHorizontalInset(viewportWidth) {
  return Math.min(64, Math.max(48, viewportWidth * 0.05))
}

export function fullPlayerGap(viewportWidth) {
  return clamp(
    viewportWidth * 0.017,
    FULL_PLAYER_DESKTOP_MIN_GAP,
    FULL_PLAYER_DESKTOP_MAX_GAP,
  )
}

export function fullPlayerRailPolicy(availableWidth) {
  const isWide = availableWidth >= FULL_PLAYER_WIDE_AVAILABLE_WIDTH
  const isSmallDesktop = availableWidth < 1440
  const min = isWide
    ? FULL_PLAYER_WIDE_RAIL_MIN
    : isSmallDesktop
      ? FULL_PLAYER_SMALL_DESKTOP_RAIL_MIN
      : FULL_PLAYER_MEDIUM_RAIL_MIN
  const max = isWide
    ? FULL_PLAYER_WIDE_RAIL_MAX
    : isSmallDesktop
      ? FULL_PLAYER_SMALL_DESKTOP_RAIL_MAX
      : FULL_PLAYER_MEDIUM_RAIL_MAX
  const preferred = Math.min(max, Math.max(min, availableWidth * 0.24))
  return {
    mode: isWide ? 'wide' : isSmallDesktop ? 'small' : 'medium',
    min,
    preferred,
    max,
  }
}

function fullPlayerRailVideoThreshold(theaterVideoWidth) {
  return Math.min(
    FULL_PLAYER_RAIL_VIDEO_TARGET_WIDTH,
    Math.max(
      FULL_PLAYER_RAIL_VIDEO_MIN_WIDTH,
      theaterVideoWidth * FULL_PLAYER_RAIL_VIDEO_RATIO,
    ),
  )
}

/**
 * Pick a preferred Stage ratio without making a ratio itself the product
 * contract. The source aspect is a hint, while the available CSS viewport
 * and the rail state keep the result inside a comfortable landscape range.
 */
export function adaptiveStageAspect({
  mediaAspect = FULL_PLAYER_ASPECT_RATIO,
  availableWidth = 0,
  heightBudget = 0,
  railOpen = false,
} = {}) {
  const sourceAspect = clamp(
    finiteOr(mediaAspect, FULL_PLAYER_ASPECT_RATIO),
    FULL_PLAYER_STAGE_ASPECT_MIN,
    FULL_PLAYER_STAGE_ASPECT_MAX,
  )
  const budget = Math.max(1, finiteOr(heightBudget, 1))
  const available = Math.max(0, finiteOr(availableWidth, 0))
  const viewportAspect = clamp(
    available > 0 ? available / budget : sourceAspect,
    FULL_PLAYER_STAGE_ASPECT_MIN,
    FULL_PLAYER_STAGE_ASPECT_MAX,
  )
  const sourceWeight = railOpen ? 0.58 : 0.68
  const viewportWeight = 1 - sourceWeight
  const railBias = railOpen ? -0.06 : 0
  return clamp(
    sourceAspect * sourceWeight + viewportAspect * viewportWeight + railBias,
    FULL_PLAYER_STAGE_ASPECT_MIN,
    FULL_PLAYER_STAGE_ASPECT_MAX,
  )
}

export function fullPlayerDesktopLayout({
  railVideoWidth,
  theaterVideoWidth,
  railPreference = 'auto',
} = {}) {
  const railWidth = Math.max(0, finiteOr(railVideoWidth, 0))
  const theaterWidth = Math.max(0, finiteOr(theaterVideoWidth, 0))
  const threshold = fullPlayerRailVideoThreshold(theaterWidth)
  const railIsReasonable = railWidth >= threshold || railWidth >= theaterWidth
  const preference = ['auto', 'shown', 'hidden'].includes(railPreference)
    ? railPreference
    : 'auto'
  // A manual desktop toggle is authoritative. Auto is the only mode that
  // applies the comfortable-video heuristic; otherwise a small desktop could
  // never open the rail after the user explicitly requested it.
  const mode = preference === 'hidden'
    ? 'theater'
    : preference === 'shown' || railIsReasonable
      ? 'rail'
      : 'theater'

  return {
    mode,
    railIsReasonable,
    railVideoWidth: railWidth,
    theaterVideoWidth: theaterWidth,
    railVideoThreshold: threshold,
    preference,
  }
}

function containMediaSize(stageWidth, stageHeight, mediaAspect) {
  const width = Math.max(0, finiteOr(stageWidth, 0))
  const height = Math.max(0, finiteOr(stageHeight, 0))
  const aspect = Number.isFinite(Number(mediaAspect)) && Number(mediaAspect) > 0
    ? Number(mediaAspect)
    : FULL_PLAYER_ASPECT_RATIO
  if (!width || !height) return { width: 0, height: 0 }
  const mediaWidth = Math.min(width, height * aspect)
  return {
    width: mediaWidth,
    height: mediaWidth / aspect,
  }
}

/**
 * Limit the extra Stage height used for Rail letterboxing. This is a visual
 * budget, not a Stage aspect contract: it lets Rail use spare vertical room
 * without turning a wide source into a tall black column.
 */
export function adaptiveRailLetterboxBudget(mediaHeight) {
  const height = Math.max(0, finiteOr(mediaHeight, 0))
  return clamp(
    height * FULL_PLAYER_RAIL_LETTERBOX_RATIO,
    FULL_PLAYER_RAIL_LETTERBOX_MIN,
    FULL_PLAYER_RAIL_LETTERBOX_MAX,
  )
}

function stageGeometry({
  stageWidth,
  heightBudget,
  stageAspect,
  mediaAspect,
  heightAware = false,
}) {
  const width = Math.max(0, finiteOr(stageWidth, 0))
  const budget = Math.max(0, finiteOr(heightBudget, 0))
  const naturalHeight = Math.max(0, Math.min(
    budget,
    width / stageAspect,
  ))
  const naturalMedia = containMediaSize(width, naturalHeight, mediaAspect)
  const letterboxBudget = heightAware
    ? adaptiveRailLetterboxBudget(naturalMedia.height)
    : 0
  const height = Math.max(0, Math.min(
    budget,
    heightAware
      ? Math.max(naturalHeight, naturalMedia.height + letterboxBudget)
      : naturalHeight,
  ))
  const media = containMediaSize(width, height, mediaAspect)
  const letterboxHeight = Math.max(0, height - media.height)
  return {
    stageWidth: width,
    stageHeight: height,
    stageAspectRatio: height > 0 ? width / height : stageAspect,
    mediaWidth: media.width,
    mediaHeight: media.height,
    letterboxTop: letterboxHeight / 2,
    letterboxBottom: letterboxHeight / 2,
    remainingVerticalWhitespace: Math.max(0, budget - height),
    letterboxBudget,
  }
}

export function fullPlayerInteractionMode({
  viewportWidth,
  hasFinePointer = false,
  hasHover = false,
  mobileMaxWidth = FULL_PLAYER_MOBILE_MAX_WIDTH,
} = {}) {
  const width = Math.max(0, finiteOr(viewportWidth, 0))
  if (width <= mobileMaxWidth) return 'mobile'
  return hasFinePointer && hasHover ? 'desktop' : 'mobile'
}

/**
 * Calculate the only FullPlayer layout authority.
 *
 * Desktop is two-column when the CSS viewport is not truly narrow and the
 * browser has a fine, hover-capable pointer. The same Desktop template scales
 * from a compact 260px rail to the wide 400px rail; it does not require a
 * fixed video-width target and it never uses devicePixelRatio for layout.
 */
export function calculateFullPlayerSizing({
  viewportWidth,
  viewportHeight,
  hasFinePointer = false,
  hasHover = false,
  mobileMaxWidth = FULL_PLAYER_MOBILE_MAX_WIDTH,
  railPreference = 'auto',
  mediaAspect = FULL_PLAYER_ASPECT_RATIO,
  layoutPaddingTop = Math.min(42, Math.max(22, finiteOr(viewportHeight, 0) * 0.035)),
  layoutPaddingBottom = Math.min(22, Math.max(12, finiteOr(viewportHeight, 0) * 0.018)),
  playerMainOffset = 6,
  nowPanelHeight = 150,
} = {}) {
  const width = Math.max(0, finiteOr(viewportWidth, 0))
  const height = Math.max(0, finiteOr(viewportHeight, 0))
  const aspect = Number.isFinite(Number(mediaAspect)) && Number(mediaAspect) > 0
    ? Number(mediaAspect)
    : FULL_PLAYER_ASPECT_RATIO
  const horizontalInset = fullPlayerHorizontalInset(width)
  const availableWidth = Math.max(0, width - horizontalInset)
  const gap = fullPlayerGap(width)
  const rail = fullPlayerRailPolicy(availableWidth)
  const mode = fullPlayerInteractionMode({
    viewportWidth: width,
    hasFinePointer,
    hasHover,
    mobileMaxWidth,
  })
  const heightBudget = Math.max(
    240,
    height
      - finiteOr(layoutPaddingTop, 0)
      - finiteOr(layoutPaddingBottom, 0)
      - finiteOr(playerMainOffset, 0)
      - Math.ceil(Math.max(0, finiteOr(nowPanelHeight, 0))),
  )
  const railAvailableWidth = Math.max(0, availableWidth - gap - rail.preferred)
  const theaterStageAspect = adaptiveStageAspect({
    mediaAspect: aspect,
    availableWidth,
    heightBudget,
    railOpen: false,
  })
  const railStageAspect = adaptiveStageAspect({
    mediaAspect: aspect,
    availableWidth: railAvailableWidth,
    heightBudget,
    railOpen: true,
  })
  const theaterWidthByHeight = heightBudget * theaterStageAspect
  const railWidthByHeight = heightBudget * railStageAspect
  const railVideoWidth = Math.max(
    0,
    Math.min(railWidthByHeight, railAvailableWidth),
  )
  const theaterVideoWidth = Math.max(0, Math.min(theaterWidthByHeight, availableWidth))
  const desktopLayout = fullPlayerDesktopLayout({
    railVideoWidth,
    theaterVideoWidth,
    railPreference,
  })

  if (mode === 'mobile') {
    return {
      mode: 'mobile',
      desktopLayout: null,
      railPreference: desktopLayout.preference,
      railIsReasonable: false,
      railWidth: 0,
      videoWidth: width,
      videoHeight: aspect > 0 ? width / aspect : 0,
      layoutWidth: width,
      availableWidth,
      horizontalInset,
      effectiveInset: 0,
      gap,
      widthByHeight: theaterWidthByHeight,
      stageWidth: width,
      stageHeight: aspect > 0 ? width / aspect : 0,
      stageAspectRatio: aspect,
      preferredStageAspectRatio: aspect,
      mediaWidth: width,
      mediaHeight: aspect > 0 ? width / aspect : 0,
      letterboxTop: 0,
      letterboxBottom: 0,
      remainingVerticalWhitespace: 0,
      letterboxBudget: 0,
      hasFinePointer,
      hasHover,
    }
  }

  const isRailLayout = desktopLayout.mode === 'rail'
  const desktopVideoWidth = isRailLayout ? railVideoWidth : theaterVideoWidth
  const stageAspect = isRailLayout ? railStageAspect : theaterStageAspect
  const stage = stageGeometry({
    stageWidth: desktopVideoWidth,
    heightBudget,
    stageAspect,
    mediaAspect: aspect,
    heightAware: isRailLayout,
  })
  const layoutGap = isRailLayout ? gap : 0
  const layoutRailWidth = isRailLayout ? rail.preferred : 0
  const layoutWidth = desktopVideoWidth + layoutGap + layoutRailWidth
  return {
    mode: 'desktop',
    desktopLayout: desktopLayout.mode,
    railMode: rail.mode,
    railPreference: desktopLayout.preference,
    railIsReasonable: desktopLayout.railIsReasonable,
    railVideoThreshold: desktopLayout.railVideoThreshold,
    railVideoWidth,
    theaterVideoWidth,
    railWidth: layoutRailWidth,
    videoWidth: desktopVideoWidth,
    videoHeight: stage.stageHeight,
    stageWidth: stage.stageWidth,
    stageHeight: stage.stageHeight,
    stageAspectRatio: stage.stageAspectRatio,
    preferredStageAspectRatio: stageAspect,
    mediaWidth: stage.mediaWidth,
    mediaHeight: stage.mediaHeight,
    letterboxTop: stage.letterboxTop,
    letterboxBottom: stage.letterboxBottom,
    remainingVerticalWhitespace: stage.remainingVerticalWhitespace,
    letterboxBudget: stage.letterboxBudget,
    layoutWidth,
    availableWidth,
    horizontalInset,
    effectiveInset: Math.max(0, (width - layoutWidth) / 2),
    gap: layoutGap,
    widthByHeight: isRailLayout ? railWidthByHeight : theaterWidthByHeight,
    theaterWidthByHeight,
    railWidthByHeight,
    hasFinePointer,
    hasHover,
  }
}
