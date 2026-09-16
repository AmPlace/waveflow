import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

test('Mobile header controls share 40px visuals with 44px hit targets', () => {
  const css = source('src/style.css')
  assert.match(css, /\.mobile-action-btn::before \{[\s\S]*?inset: 2px;/)
  assert.match(css, /\.mobile-action-btn > svg \{[\s\S]*?width: 1\.125rem;[\s\S]*?height: 1\.125rem;/)
  assert.match(css, /\.mobile-search-shell \{[\s\S]*?background: transparent;[\s\S]*?box-shadow: none;/)
  assert.match(css, /\.mobile-search-collapsed::before \{[\s\S]*?inset: 2px;/)
  assert.match(css, /\.mobile-search-collapsed::before \{[\s\S]*?box-shadow: 0 2px 8px rgba\(0, 0, 0, 0\.06\);/)
  assert.match(css, /\.mobile-search-expanded \{[\s\S]*?border-width: 1px;/)
  assert.match(css, /\.mobile-search-trigger \{[\s\S]*?width: 2\.75rem;[\s\S]*?height: 2\.75rem;/)
  assert.doesNotMatch(source('src/App.vue'), /mobile-search-shell[^>]*shadow-sm|mobile-mode-switch[^>]*shadow-sm/)
})

test('Radio/TV segmented control remains a compact 14px control', () => {
  const app = source('src/App.vue')
  assert.match(app, /mobile-mode-switch flex h-10[\s\S]*text-sm/)
  assert.equal((app.match(/class="flex h-9 items-center rounded-full px-3 transition-all sm:px-4"/g) || []).length, 2)
})

test('IPTV filter chips keep 44px interaction boxes with 40px visual pills', () => {
  const tag = source('src/components/TagFilterRow.vue')
  const css = source('src/style.css')

  assert.match(tag, /tag-filter-row__item/)
  assert.match(tag, /tag-filter-row__item--selected/)
  assert.match(tag, /tag-filter-row__more/)
  assert.match(tag, /const collapsedItems = computed\(/)
  assert.match(tag, /const displayItems = computed\(\(\) => \(expanded\.value \? props\.items : collapsedItems\.value\)\)/)
  assert.doesNotMatch(tag, /<Transition name="chip">/)
  assert.doesNotMatch(tag, /ml-auto/)
  assert.match(tag, /height: expanded \? `\$\{Math\.max\(expandedHeight, 44\)\}px` : '44px'/)
  assert.match(tag, /transition: 'height 300ms cubic-bezier\(0\.4, 0, 0\.2, 1\)'/)
  assert.match(tag, /e\.propertyName === 'height'/)
  assert.match(css, /\.iptv-main \.tag-filter-row__item,[\s\S]*?height: 2\.75rem;/)
  assert.match(css, /\.iptv-main \.tag-filter-row__item::before,[\s\S]*?inset: 2px;/)
})

test('IPTV density is a direct toggle and section controls retain their presentation role', () => {
  const home = source('src/views/IptvHome.vue')
  const css = source('src/style.css')

  assert.match(home, /class="iptv-density-toggle"/)
  assert.match(home, /data-density-toggle/)
  assert.match(home, /:aria-pressed="densityMode === 'standard'"/)
  assert.match(home, /@click="toggleDensityMode"/)
  assert.doesNotMatch(home, /<details class="iptv-density-menu"|role="switch"|data-density-option=/)
  assert.match(home, /class="iptv-sort-trigger inline-flex h-10/)
  assert.match(css, /\.iptv-main \.iptv-density-toggle::before \{[\s\S]*?inset: 2px;/)
  assert.match(css, /\.iptv-main \.iptv-sort-trigger::before \{[\s\S]*?inset: 2px;/)
  assert.match(css, /\.iptv-main \.iptv-sort-trigger > svg \{[\s\S]*?width: 1\.125rem;/)
})

test('Mini Player only exposes play, mute and FullPlayer controls', () => {
  const player = source('src/components/BottomPlayer.vue')
  const css = source('src/style.css')

  for (const token of ['mobile-player-shell', 'mobile-player-logo', 'mobile-player-title', 'mobile-player-status', 'mobile-player-play-btn', 'mobile-player-action-control', 'mobile-player-action-icon']) assert.match(player, new RegExp(token))
  assert.match(player, /mobile-player-shell[^>]*h-\[72px\][\s\S]*?lg:h-16/)
  assert.match(player, /mobile-player-info flex min-w-0 basis-\[40%\]/)
  assert.match(player, /mobile-player-status mt-0\.5/)
  assert.match(css, /\.mobile-player-action-icon \{[\s\S]*?width: 1\.25rem;[\s\S]*?height: 1\.25rem;/)
  assert.match(css, /\.mobile-player-shell \{[\s\S]*?border-radius: var\(--panel-radius\);[\s\S]*?box-shadow: var\(--floating-shadow\);/)
  assert.match(player, /@click="openFullPlayer"/)
  assert.match(player, /@click\.stop="playerStore\.togglePlay\(\)"/)
  assert.match(player, /@click\.stop="toggleMute"/)
  assert.match(player, /@click\.stop="openFullPlayer"/)
  assert.match(player, /mobile-player-play-icon--play/)
  assert.match(player, /<path d="m6 14 6-6 6 6"/)
  assert.doesNotMatch(player, /aria-label="上一个"|aria-label="下一个"|节目列表|mobile-player-volume|dock-list-btn/)
})

test('Desktop Mini Player identity text stays visually below the logo', () => {
  const player = source('src/components/BottomPlayer.vue')
  assert.match(player, /mobile-player-title[^>]*lg:text-\[13px\]/)
  assert.match(player, /mobile-player-status[^>]*lg:text-\[11px\]/)
  assert.match(player, /mobile-player-logo[^>]*lg:size-10/)
})

test('Mini Player mute state is shared with FullPlayer and has accessible controls', () => {
  const player = source('src/components/BottomPlayer.vue')
  const fullPlayer = source('src/components/FullPlayer.vue')
  const store = source('src/stores/player.js')
  const audio = source('src/components/AudioEngine.vue')

  assert.match(player, /:aria-label="isMuted \? '取消静音' : '静音'"/)
  assert.match(player, /:aria-pressed="isMuted"/)
  assert.match(fullPlayer, /:aria-label="iptvMuted \? '取消静音' : '静音'"/)
  assert.match(fullPlayer, /playerStore\.initializeMuted\(isIOS\)/)
  assert.match(store, /isMuted: false/)
  assert.match(store, /setMuted\(nextMuted\)/)
  assert.match(audio, /audioRef\.value\.muted = isMuted\.value/)
})

test('Mini Player uses compact desktop geometry and one occupied-height authority', () => {
  const player = source('src/components/BottomPlayer.vue')
  const css = source('src/style.css')

  assert.match(player, /lg:h-16/)
  assert.match(player, /lg:size-12/)
  assert.match(css, /--mini-player-occupied-height: calc\(var\(--mini-player-height\) \+ var\(--mini-player-bottom-offset\) \+ var\(--mini-player-content-gap\)\)/)
  assert.match(css, /\.page-with-mini-player \{\s*padding-bottom: var\(--mini-player-occupied-height\);/)
  assert.match(css, /@media \(min-width: 1024px\) \{[\s\S]*?--mini-player-height: 64px;[\s\S]*?\.bottom-player-dock \{[\s\S]*?max-width: 600px;/)
  assert.match(css, /\.mobile-player-play-icon--play \{\s*transform: translateX\(-1px\);/)
})

test('All shell pages reserve the shared Mini Player occupied height', () => {
  for (const relativePath of [
    'src/views/Home.vue',
    'src/views/IptvHome.vue',
    'src/views/MarketView.vue',
    'src/views/settings/SettingsView.vue',
  ]) {
    const view = source(relativePath)
    assert.match(view, /page-with-mini-player/)
    assert.doesNotMatch(view, /pb-32|lg:pb-40/)
  }
})
