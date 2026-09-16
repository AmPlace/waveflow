import assert from 'node:assert/strict'
import { test } from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const fullPlayer = fs.readFileSync(path.join(frontendRoot, 'src/components/FullPlayer.vue'), 'utf8')
const template = fullPlayer.split('<script setup>')[0]

test('FullPlayer Desktop overlay owns programme progress and keeps it non-seekable', () => {
  assert.match(template, /class="video-overlay desktop-video-overlay"/)
  const progressBlock = template.match(/class="program-progress video-overlay-progress"[\s\S]*?<div class="video-overlay-controls">/)?.[0] || ''
  assert.match(progressBlock, /role="progressbar"/)
  assert.doesNotMatch(progressBlock, /<input/)
  assert.doesNotMatch(progressBlock, /progress-knob/)
  assert.match(template, /class="video-overlay-button video-overlay-button--main"[\s\S]*?@click="playerStore\.togglePlay\(\)"/)
  assert.match(template, /class="video-overlay-button video-overlay-button--utility"[\s\S]*?aria-label="全屏"/)
})

test('FullPlayer channel rows use logical identity instead of sorted index keys', () => {
  assert.match(fullPlayer, /key: `iptv-\$\{channelIdentity\(ch\) \|\| ch\.canonical_key \|\| ch\.name\}`/)
  assert.doesNotMatch(fullPlayer, /key: `iptv-\$\{ch\.name\}-\$\{index\}`/)
})

test('FullPlayer video-first geometry has one sizing authority and no fixed rail jump', () => {
  assert.match(fullPlayer, /--layout-gap: clamp\(20px, 1\.7vw, 24px\)/)
  assert.match(fullPlayer, /--channel-logo-size: 48px/)
  assert.match(fullPlayer, /--channel-min-height: 64px/)
  assert.match(fullPlayer, /minmax\(300px, 400px\)/)
  assert.match(fullPlayer, /calculateFullPlayerSizing\(/)
  assert.match(fullPlayer, /full-player--mobile-layout/)
  assert.doesNotMatch(fullPlayer, /const isWideViewport = viewportWidth >= 1440/)
})

test('FullPlayer 1.5 overlay provides delayed loading, status, and unified volume semantics', () => {
  assert.match(template, /class="video-loading-indicator"/)
  assert.match(template, /class="video-loading-spinner"/)
  assert.match(template, /class="video-playback-failure"[\s\S]*?:aria-label="overlayPlaybackStatusText"/)
  assert.match(template, /class="video-playback-failure-label">\{\{ overlayPlaybackStatusText \}\}/)
  assert.match(template, /class="video-overlay-button video-overlay-button--main video-playback-failure-retry"[\s\S]*?aria-label="重新播放"/)
  assert.match(fullPlayer, /const showPlaybackFailureIndicator = computed/)
  assert.match(fullPlayer, /async function retryPlayback\(\)/)
  assert.match(template, /class="video-overlay-status"/)
  assert.match(template, /class="video-overlay-volume-panel"/)
  assert.match(template, /:value="effectiveVolume"/)
  assert.match(template, /@input="setOverlayVolume\(\$event\.target\.value\)"/)
  assert.match(fullPlayer, /setTimeout\(\(\) => \{[\s\S]*?isOverlayLoadingCandidate\.value/s)
  assert.match(fullPlayer, /const overlayPlaybackStatusText = computed/)
  assert.match(fullPlayer, /const volumeIconState = computed/)
})

test('FullPlayer failure retry keeps desktop and mobile main control sizes', () => {
  assert.match(fullPlayer, /\.video-loading-spinner \{[\s\S]*?width: 48px;[\s\S]*?height: 48px;/)
  assert.match(fullPlayer, /\.video-playback-failure-icon \{[\s\S]*?width: 48px;[\s\S]*?height: 48px;[\s\S]*?color: rgba\(255, 255, 255, 0\.94\);/)
  assert.match(fullPlayer, /\.video-playback-failure-label \{[\s\S]*?font-size: 15px;/)
  assert.match(fullPlayer, /\.video-overlay-button\.video-playback-failure-retry \{[\s\S]*?width: 48px;[\s\S]*?height: 48px;/)
  assert.match(fullPlayer, /\.video-playback-failure-retry svg \{[\s\S]*?width: 24px;[\s\S]*?height: 24px;/)
  assert.match(fullPlayer, /\.video-overlay-button\.video-playback-failure-retry \{[\s\S]*?background: rgba\(15, 23, 42, 0\.62\);/)
  assert.doesNotMatch(fullPlayer, /\.video-overlay-button\.video-playback-failure-retry \{[\s\S]*?background: rgba\(255, 255, 255, 0\.94\);/)
  assert.match(fullPlayer, /\.full-player--mobile-layout \.video-playback-failure-retry \{[\s\S]*?width: 48px;[\s\S]*?height: 48px;/)
  assert.match(fullPlayer, /\.full-player--mobile-layout \.video-playback-failure-retry svg \{[\s\S]*?width: 22px;[\s\S]*?height: 22px;/)
})

test('Mobile FullPlayer consolidates transport and utility controls into the video overlay', () => {
  assert.match(template, /v-if="isMobileLayout"[\s\S]*?class="video-overlay mobile-video-overlay"/)
  assert.match(template, /class="program-progress mobile-video-overlay-progress"[\s\S]*?role="progressbar"/)
  assert.match(template, /class="mobile-video-overlay-transport"[\s\S]*?aria-label="上一个"[\s\S]*?aria-label="下一个"/)
  assert.match(template, /class="mobile-video-overlay-status"[\s\S]*?overlayPlaybackStatusText/)
  assert.match(template, /class="mobile-video-overlay-button mobile-video-overlay-button--utility"[\s\S]*?aria-label="切换播放源"/)
  assert.match(fullPlayer, /const isMobileOverlayHidden = computed/)
  assert.match(fullPlayer, /function scheduleMobileOverlayHide\(\)/)
  assert.doesNotMatch(template, /mobile-now-controls/)
})

test('Mobile FullPlayer overlay keeps transport centered and utility controls transparent', () => {
  const mobileStyles = fullPlayer.match(/\.full-player--mobile-layout \.mobile-video-overlay \{[\s\S]*?\.full-player--mobile-layout \.media-video \{/)?.[0] || ''
  assert.match(mobileStyles, /\.full-player--mobile-layout \.mobile-video-overlay-transport \{[\s\S]*?position: absolute;[\s\S]*?top: 50%;[\s\S]*?left: 50%;[\s\S]*?transform: translate\(-50%, -50%\)/)
  assert.match(mobileStyles, /\.full-player--mobile-layout \.mobile-video-overlay-bottom \{[\s\S]*?position: absolute;[\s\S]*?bottom: 10px;/)
  assert.match(mobileStyles, /\.full-player--mobile-layout \.mobile-video-overlay-button--utility \{[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/)
  assert.match(mobileStyles, /\.full-player--mobile-layout \.mobile-video-overlay\.is-loading \.mobile-video-overlay-transport,[\s\S]*?opacity: 0;[\s\S]*?pointer-events: none;/)
})

test('Mobile IPTV overlay controls use one restrained glass family', () => {
  const glassStyles = fullPlayer.match(/\/\* Mobile IPTV overlay controls share the existing MiniPlayer glass language\. \*\/[\s\S]*?<\/style>/)?.[0] || ''
  assert.match(glassStyles, /--mobile-overlay-glass-source: rgba\(15, 23, 42, 0\.42\)/)
  assert.match(glassStyles, /backdrop-filter: blur\(12px\) saturate\(110%\)/)
  assert.match(glassStyles, /\.full-player--mobile-iptv \.overlay-btn,[\s\S]*?border: 1px solid var\(--mobile-overlay-glass-border\)/)
  assert.match(glassStyles, /\.full-player--mobile-iptv \.mobile-video-overlay-button--main \{[\s\S]*?background: var\(--mobile-overlay-glass-source\)/)
  assert.match(glassStyles, /\.full-player--mobile-iptv \.mobile-video-overlay-button--utility \{[\s\S]*?background: var\(--mobile-overlay-glass-surface-soft\)/)
  assert.match(glassStyles, /\.full-player--mobile-iptv \.mobile-video-overlay-button--side \{[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/)
  assert.match(glassStyles, /\.full-player--mobile-iptv \.overlay-btn:focus-visible,[\s\S]*?outline: 2px solid rgba\(255, 255, 255, 0\.86\)/)
  assert.doesNotMatch(glassStyles, /background: rgba\(255, 255, 255, 0\.94\)/)
})

test('Mobile IPTV uses a compact now-playing row and a sticky unified panel header', () => {
  assert.match(template, /'full-player--mobile-iptv': isMobileLayout && isIptvMode/)
  assert.match(template, /v-if="isMobileLayout && isIptvMode" class="mobile-now-playing"/)
  assert.match(template, /class="mobile-now-playing__logo"[\s\S]*?currentArtworkUrl/)
  assert.match(template, /class="mobile-now-playing__programme" aria-live="polite"/)
  assert.match(template, /class="mobile-panel-header"[\s\S]*?class="mobile-panel-sort"/)
  assert.match(template, /class="mobile-panel-header"[\s\S]*?currentSortLabel/)
  assert.match(template, /class="schedule-panel" :class="\{ 'schedule-panel--empty': !hasScheduleData \}"/)
  assert.match(fullPlayer, /\.full-player--mobile-iptv \.mobile-panel-header \{[\s\S]*?position: sticky;[\s\S]*?top: env\(safe-area-inset-top, 0px\)/)
  assert.match(fullPlayer, /\.full-player--mobile-iptv \.mobile-panel-header \.panel-tabs button \{[\s\S]*?min-height: 44px;/)
  assert.match(fullPlayer, /\.full-player--mobile-iptv \.mobile-panel-sort \.sort-btn \{[\s\S]*?min-height: 44px;/)
  assert.match(fullPlayer, /\.full-player--mobile-iptv \.schedule-panel--empty \{[\s\S]*?padding: 0;/)
})

test('FullPlayer 1.5 desktop metadata uses structured next-programme fields', () => {
  assert.match(template, /:class="\{ 'now-metadata--without-programme': !hasProgrammeMetadata \}"/)
  assert.match(template, /v-if="hasProgrammeMetadata" class="now-programme"/)
  assert.match(template, /class="desktop-now-program-next"[\s\S]*?下一节目[\s\S]*?desktop-now-program-next__title/)
  assert.match(fullPlayer, /\.now-panel \.mobile-now-program-next/)
  assert.match(fullPlayer, /\.now-panel \.desktop-now-program-next/)
  assert.doesNotMatch(fullPlayer, /暂无节目单/)
})

test('FullPlayer overlay activity hides only stable playback and restores on interaction', () => {
  const rootOpen = template.match(/<div\s+v-show="isPlayerExpanded"[\s\S]*?>/)?.[0] || ''
  const mediaOpen = template.match(/<section\s+ref="mediaSurfaceRef"[\s\S]*?>/)?.[0] || ''
  assert.doesNotMatch(rootOpen, /@pointerenter|@pointermove|@mousemove|@pointerdown|@touchstart/)
  assert.match(mediaOpen, /@pointerenter="handleOverlayActivity"/)
  assert.match(mediaOpen, /@pointermove="handleOverlayActivity"/)
  assert.match(mediaOpen, /@mousemove="handleOverlayActivity"/)
  assert.match(mediaOpen, /@pointerdown="handlePlayerPointerDown"/)
  assert.match(mediaOpen, /@touchstart="handlePlayerTouchStart"/)
  assert.match(template, /@keydown\.capture="handlePlayerKeyboard"/)
  assert.match(template, /@focusin\.capture="handleOverlayFocusIn"/)
  assert.match(template, /:class="\{ 'is-hidden': isDesktopOverlayHidden \}"/)
  assert.match(fullPlayer, /}, 2800\)/)
  assert.match(fullPlayer, /}, 900\)/)
  assert.match(fullPlayer, /overlayControlsFocused\.value/)
  assert.match(fullPlayer, /overlayVolumeInteracting\.value/)
  assert.match(fullPlayer, /sourceMenuOpen\.value/)
  assert.match(fullPlayer, /lastOverlayInputModality/)
  assert.match(fullPlayer, /isMediaPlayerControlTarget\(target\)/)
  assert.match(fullPlayer, /overlayControlsFocused\.value = true/)
})

test('FullPlayer desktop shortcuts reuse the existing capture path without a second global keydown listener', () => {
  assert.match(template, /ref="playerRootRef"[\s\S]*?tabindex="-1"/)
  assert.match(fullPlayer, /function handlePlayerKeyboard\(event\)/)
  assert.match(fullPlayer, /const KEYBOARD_VOLUME_STEP = 0\.05/)
  assert.match(fullPlayer, /code === 'Space' \|\| code === 'KeyK'/)
  assert.match(fullPlayer, /code === 'KeyM'/)
  assert.match(fullPlayer, /code === 'KeyF'/)
  assert.match(fullPlayer, /code === 'PageUp' \|\| code === 'PageDown'/)
  assert.match(fullPlayer, /event\.repeat && !isVolumeShortcut/)
  assert.match(fullPlayer, /event\.preventDefault\(\)/)
  assert.match(fullPlayer, /input, textarea, select, option, button, a/)
  assert.doesNotMatch(fullPlayer, /addEventListener\(['"]keydown['"]/)
})

test('FullPlayer custom fullscreen targets the media wrapper and tracks fullscreenchange', () => {
  assert.match(template, /ref="mediaSurfaceRef"[\s\S]*?class="media-card"/)
  assert.match(template, /ref="mediaSurfaceRef"[\s\S]*?tabindex="-1"/)
  assert.match(fullPlayer, /document\.fullscreenEnabled === true/)
  assert.match(fullPlayer, /typeof target\?\.requestFullscreen === 'function'/)
  assert.match(fullPlayer, /typeof video\?\.webkitEnterFullscreen === 'function'/)
  assert.match(fullPlayer, /document\.addEventListener\('fullscreenchange', handleFullscreenChange\)/)
  assert.match(fullPlayer, /document\.removeEventListener\('fullscreenchange', handleFullscreenChange\)/)
  assert.match(fullPlayer, /mediaSurfaceRef\.value\.contains\(fullscreenElement\)/)
  assert.match(fullPlayer, /isFullscreen\.value \? mediaSurfaceRef\.value : playerRootRef\.value/)
  assert.match(fullPlayer, /isFullscreen\.value = false/)
  const videoTag = template.match(/<video[\s\S]*?<\/video>/)?.[0] || ''
  assert.doesNotMatch(videoTag, /\bcontrols(?:=|\s)/)
})

test('FullPlayer media video stays out of pointer hit testing so surrounding controls remain interactive', () => {
  assert.match(fullPlayer, /\.media-video \{[\s\S]*?pointer-events: none;/)
  assert.match(template, /class="desktop-collapse-btn"[\s\S]*?@click="playerStore\.collapsePlayer\(\)"/)
})

test('FullPlayer separates source selector and Desktop channel rail controls', () => {
  assert.match(template, /aria-label="切换播放源"[\s\S]*?m4\.5 7 7\.5-3 7\.5 3/s)
  assert.match(template, /:aria-label="isFullscreen \? '全屏中不可显示频道列表' : \(isRailLayout \? '隐藏频道列表' : '显示频道列表'\)"/)
  assert.match(template, /:aria-expanded="isRailLayout"/)
  assert.match(fullPlayer, /function toggleDesktopRail\(\)/)
  assert.match(fullPlayer, /desktopRailPreference\.value = isRailLayout\.value \? 'hidden' : 'shown'/)
  assert.match(fullPlayer, /full-player--theater/)
  assert.match(fullPlayer, /sourceMenuTeleportTarget/)
  assert.match(fullPlayer, /source-menu-in-fullscreen/)
})

test('FullPlayer side channel rail uses the shared panel transition when opening and closing', () => {
  assert.match(fullPlayer, /\.player-layout \{[\s\S]*?transition: grid-template-columns 320ms cubic-bezier\(0\.22, 1, 0\.36, 1\),[\s\S]*?column-gap 320ms cubic-bezier\(0\.22, 1, 0\.36, 1\);/)
  assert.match(fullPlayer, /\.full-player\.full-player--theater \.player-layout \{[\s\S]*?grid-template-columns: minmax\(0, var\(--media-frame-width, 1fr\)\) 0fr;[\s\S]*?column-gap: 0;/)
  assert.match(fullPlayer, /\.full-player\.full-player--theater \.side-panel \{[\s\S]*?opacity: 0;[\s\S]*?transform: translate3d\(12px, 0, 0\);[\s\S]*?visibility: hidden;/)
  assert.doesNotMatch(fullPlayer, /\.full-player\.full-player--theater \.side-panel \{\s*display: none;/)
})

test('Desktop rail keeps its presentation rules scoped away from Mobile FullPlayer', () => {
  assert.match(fullPlayer, /\.side-panel \{[\s\S]*?--channel-logo-size: 46px;[\s\S]*?--channel-min-height: 62px;/)
  assert.match(fullPlayer, /\.side-panel \.channel-logo img \{[\s\S]*?object-fit: contain;/)
  assert.match(fullPlayer, /\.channel-logo img \{[\s\S]*?object-fit: cover;/)
  assert.match(fullPlayer, /\.channel-logo \{[\s\S]*?width: var\(--channel-logo-size\);[\s\S]*?height: var\(--channel-logo-size\);[\s\S]*?overflow: hidden;/)
  assert.match(fullPlayer, /\.side-panel \.channel-row\.active \{[\s\S]*?box-shadow: inset 2px 0 0 var\(--accent\)/)
  assert.match(fullPlayer, /\.side-panel \.sort-btn:focus-visible/)
  assert.match(fullPlayer, /\.desktop-panel-scroll::-webkit-scrollbar/)
  assert.match(fullPlayer, /class="channel-title-text">\{\{ item\.name \}\}<\/span>/)
})

test('Desktop rail reuses shared logo modes and gives wide marks horizontal optical space', () => {
  assert.match(fullPlayer, /import \{ useLogoVisual \} from '..\/composables\/useLogoVisual'/)
  assert.match(fullPlayer, /logoVisualMode: railLogoVisualMode/)
  assert.match(fullPlayer, /classifyLogo: classifyRailLogo/)
  assert.match(fullPlayer, /enableWide: true/)
  assert.match(template, /class="channel-logo" :class="`channel-logo--\$\{railLogoVisualMode\(item\)\}`"/)
  assert.match(template, /:class="`channel-logo-image--\$\{railLogoVisualMode\(item\)\}`"[\s\S]*?@load="classifyRailLogo\(item, \$event\)"/)
  assert.match(fullPlayer, /--channel-grid: 64px minmax\(0, 1fr\) 22px/)
  assert.match(fullPlayer, /--channel-logo-width: 64px;/)
  assert.match(fullPlayer, /--channel-logo-height: 46px;/)
  assert.match(fullPlayer, /\.side-panel \.channel-logo img \{[\s\S]*?width: 44px;[\s\S]*?height: 44px;[\s\S]*?object-fit: contain;/)
  assert.match(fullPlayer, /\.side-panel \.channel-logo--wide img \{[\s\S]*?width: 60px;[\s\S]*?height: 44px;/)
  assert.match(fullPlayer, /\.side-panel \.channel-logo--cover img \{[\s\S]*?width: 44px;[\s\S]*?height: 44px;/)
})

test('Desktop rail uses a light neutral logo plate without changing dark geometry', () => {
  assert.match(fullPlayer, /\.side-panel \.channel-logo \{[\s\S]*?width: var\(--channel-logo-width\);[\s\S]*?height: var\(--channel-logo-height\);[\s\S]*?background: #f1f2f3;[\s\S]*?border: 1px solid rgba\(15, 23, 42, 0\.1\);/)
  assert.match(fullPlayer, /\.full-player\.theme-dark \.side-panel \.channel-logo \{[\s\S]*?background: var\(--surface-bg\);[\s\S]*?border-color: transparent;/)
  assert.match(fullPlayer, /--channel-logo-width: 64px;/)
  assert.match(fullPlayer, /--channel-logo-height: 46px;/)
  assert.match(fullPlayer, /\.side-panel \.channel-logo img \{[\s\S]*?object-fit: contain;/)
})

test('Desktop rail adds a light-only logo edge without changing dark rendering', () => {
  assert.match(fullPlayer, /\.side-panel \.channel-logo img \{[\s\S]*?filter: drop-shadow\(0 0 1px rgba\(15, 23, 42, 0\.52\)\) drop-shadow\(0 1px 1px rgba\(15, 23, 42, 0\.18\)\);/)
  assert.match(fullPlayer, /\.full-player\.theme-dark \.side-panel \.channel-logo img \{[\s\S]*?filter: none;/)
})
