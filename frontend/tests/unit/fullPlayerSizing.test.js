import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  FULL_PLAYER_MOBILE_MAX_WIDTH,
  FULL_PLAYER_STAGE_ASPECT_MAX,
  FULL_PLAYER_STAGE_ASPECT_MIN,
  adaptiveStageAspect,
  adaptiveRailLetterboxBudget,
  calculateFullPlayerSizing,
  fullPlayerDesktopLayout,
  fullPlayerInteractionMode,
} from '../../src/utils/fullPlayerSizing.js'

const sizingAt = (viewportWidth, viewportHeight = 900, capabilities = { hasFinePointer: true, hasHover: true }) => calculateFullPlayerSizing({
  viewportWidth,
  viewportHeight,
  nowPanelHeight: 69,
  ...capabilities,
})

test('fine hover-capable Desktop browsers keep the Desktop template at common CSS widths', () => {
  const at1440 = sizingAt(1440)
  const at1600 = sizingAt(1600)

  assert.equal(at1440.mode, 'desktop')
  assert.equal(at1440.desktopLayout, 'theater')
  assert.equal(at1440.railWidth, 0)
  assert.equal(at1440.railMode, 'small')
  assert.ok(at1440.railVideoWidth >= 900 && at1440.railVideoWidth < 1100)
  assert.ok(at1440.videoWidth > at1440.railVideoWidth)
  assert.equal(at1600.mode, 'desktop')
  assert.equal(at1600.desktopLayout, 'rail')
  assert.ok(at1600.railWidth >= 300)
})

test('truly narrow CSS viewports remain Mobile regardless of pointer capability', () => {
  const narrow = sizingAt(FULL_PLAYER_MOBILE_MAX_WIDTH, 900, { hasFinePointer: true, hasHover: true })

  assert.equal(narrow.mode, 'mobile')
  assert.equal(narrow.railWidth, 0)
  assert.equal(narrow.videoWidth, FULL_PLAYER_MOBILE_MAX_WIDTH)
})

test('coarse or non-hover input prefers the existing Mobile template in the middle range', () => {
  const coarse = sizingAt(1440, 900, { hasFinePointer: false, hasHover: false })

  assert.equal(coarse.mode, 'mobile')
  assert.equal(coarse.railWidth, 0)
})

test('fine pointer keeps a compact Desktop rail at iPad-sized CSS widths without DPR scaling', () => {
  const smallDesktop = sizingAt(1024, 768, { hasFinePointer: true, hasHover: true })

  assert.equal(smallDesktop.mode, 'desktop')
  assert.equal(smallDesktop.railMode, 'small')
  assert.equal(smallDesktop.desktopLayout, 'theater')
  assert.equal(smallDesktop.railWidth, 0)
  assert.ok(smallDesktop.railVideoWidth > 600)
})

test('desktop sizing retains viewport-height protection on ultrawide screens', () => {
  const at2560 = sizingAt(2560)

  assert.equal(at2560.mode, 'desktop')
  assert.equal(at2560.desktopLayout, 'rail')
  assert.equal(at2560.railWidth, 400)
  assert.ok(at2560.videoWidth < 1500)
  assert.ok(at2560.effectiveInset > 300)
})

test('video-first sizing naturally selects the wide rail from available width', () => {
  const at1600 = sizingAt(1600)
  const at1800 = sizingAt(1800)

  assert.equal(at1600.railMode, 'medium')
  assert.equal(at1800.railMode, 'wide')
  assert.equal(at1600.desktopLayout, 'rail')
  assert.equal(at1800.desktopLayout, 'rail')
  assert.ok(at1800.railWidth >= at1600.railWidth)
})

test('Desktop Rail/Theater policy uses candidate sizing and supports session overrides', () => {
  const small = fullPlayerDesktopLayout({ railVideoWidth: 1050, theaterVideoWidth: 1368 })
  const large = fullPlayerDesktopLayout({ railVideoWidth: 1170, theaterVideoWidth: 1518 })

  assert.equal(small.mode, 'theater')
  assert.equal(fullPlayerDesktopLayout({ ...small, railPreference: 'shown' }).mode, 'rail')
  assert.equal(fullPlayerDesktopLayout({ ...small, railPreference: 'hidden' }).mode, 'theater')
  assert.equal(large.mode, 'rail')
  assert.equal(fullPlayerDesktopLayout({ ...large, railPreference: 'hidden' }).mode, 'theater')
  assert.equal(fullPlayerDesktopLayout({ ...large, railPreference: 'shown' }).mode, 'rail')
})

test('a manually expanded small Desktop uses a taller dark Stage without stretching media', () => {
  const expanded = sizingAt(1024, 900)
  const rail = calculateFullPlayerSizing({
    viewportWidth: 1024,
    viewportHeight: 900,
    nowPanelHeight: 69,
    hasFinePointer: true,
    hasHover: true,
    railPreference: 'shown',
  })

  assert.equal(expanded.desktopLayout, 'theater')
  assert.equal(rail.desktopLayout, 'rail')
  assert.ok(rail.stageHeight > rail.mediaHeight)
  assert.ok(rail.stageAspectRatio > 0)
  assert.ok(rail.preferredStageAspectRatio >= FULL_PLAYER_STAGE_ASPECT_MIN)
  assert.ok(rail.preferredStageAspectRatio <= FULL_PLAYER_STAGE_ASPECT_MAX)
  assert.ok(rail.stageAspectRatio <= rail.preferredStageAspectRatio)
  assert.ok(Math.abs(rail.mediaWidth / rail.mediaHeight - 16 / 9) < 0.000001)
  assert.ok(rail.mediaWidth <= rail.stageWidth)
  assert.ok(rail.mediaHeight <= rail.stageHeight)
  assert.equal(rail.stageWidth, rail.railVideoWidth)
  assert.ok(rail.stageHeight > rail.railVideoWidth / rail.preferredStageAspectRatio)
  assert.ok(Math.abs(rail.letterboxTop - rail.letterboxBottom) < 0.000001)
  assert.ok(Math.abs(rail.stageHeight - rail.mediaHeight - rail.letterboxTop - rail.letterboxBottom) < 0.000001)
  assert.ok(rail.remainingVerticalWhitespace >= 0)
})

test('Rail Expanded uses available height within an adaptive letterbox budget', () => {
  const rail = calculateFullPlayerSizing({
    viewportWidth: 1440,
    viewportHeight: 900,
    nowPanelHeight: 69,
    hasFinePointer: true,
    hasHover: true,
    railPreference: 'shown',
  })
  const naturalHeight = rail.railVideoWidth / rail.preferredStageAspectRatio

  assert.equal(rail.desktopLayout, 'rail')
  assert.ok(rail.stageHeight > naturalHeight)
  assert.ok(rail.remainingVerticalWhitespace < 80)
  assert.ok(rail.letterboxTop > 0)
  assert.ok(rail.letterboxBudget >= adaptiveRailLetterboxBudget(rail.mediaHeight) - 0.001)
})

test('the Desktop Stage remains aspect-safe when the rail is open at large widths', () => {
  for (const width of [1200, 1280, 1440, 1600, 1800, 1920, 2560]) {
    const sizing = calculateFullPlayerSizing({
      viewportWidth: width,
      viewportHeight: 900,
      nowPanelHeight: 69,
      hasFinePointer: true,
      hasHover: true,
      railPreference: 'shown',
    })
    assert.equal(sizing.mode, 'desktop')
    assert.ok(sizing.mediaWidth <= sizing.stageWidth + 0.001)
    assert.ok(sizing.mediaHeight <= sizing.stageHeight + 0.001)
    assert.ok(Math.abs(sizing.mediaWidth / sizing.mediaHeight - 16 / 9) < 0.000001)
    assert.ok(Math.abs(sizing.stageHeight - sizing.mediaHeight - sizing.letterboxTop - sizing.letterboxBottom) < 0.000001)
    assert.ok(sizing.remainingVerticalWhitespace >= 0)
  }
})

test('interaction mode has only Desktop and Mobile outcomes', () => {
  assert.equal(fullPlayerInteractionMode({ viewportWidth: 390, hasFinePointer: true, hasHover: true }), 'mobile')
  assert.equal(fullPlayerInteractionMode({ viewportWidth: 1024, hasFinePointer: true, hasHover: true }), 'desktop')
  assert.equal(fullPlayerInteractionMode({ viewportWidth: 1024, hasFinePointer: false, hasHover: false }), 'mobile')
})

test('Stage aspect adapts to media, available width, and rail state inside a bounded range', () => {
  const theater = adaptiveStageAspect({
    mediaAspect: 4 / 3,
    availableWidth: 1280,
    heightBudget: 720,
    railOpen: false,
  })
  const rail = adaptiveStageAspect({
    mediaAspect: 2,
    availableWidth: 900,
    heightBudget: 720,
    railOpen: true,
  })

  assert.ok(theater >= FULL_PLAYER_STAGE_ASPECT_MIN)
  assert.ok(theater <= FULL_PLAYER_STAGE_ASPECT_MAX)
  assert.ok(rail >= FULL_PLAYER_STAGE_ASPECT_MIN)
  assert.ok(rail <= FULL_PLAYER_STAGE_ASPECT_MAX)
  assert.notEqual(theater, rail)
})

test('Stage contains non-16:9 media without changing its source aspect', () => {
  for (const mediaAspect of [4 / 3, 16 / 9, 2]) {
    const sizing = calculateFullPlayerSizing({
      viewportWidth: 1440,
      viewportHeight: 900,
      nowPanelHeight: 69,
      hasFinePointer: true,
      hasHover: true,
      railPreference: 'shown',
      mediaAspect,
    })
    assert.equal(sizing.mode, 'desktop')
    assert.ok(sizing.mediaWidth <= sizing.stageWidth + 0.001)
    assert.ok(sizing.mediaHeight <= sizing.stageHeight + 0.001)
    assert.ok(Math.abs(sizing.mediaWidth / sizing.mediaHeight - mediaAspect) < 0.000001)
  }
})
