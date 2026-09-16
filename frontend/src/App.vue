<template>
  <div
    ref="scrollRef"
    class="app-root h-dvh overflow-y-auto font-sans text-[var(--text-primary)] antialiased transition-colors duration-300"
    :class="{ 'desktop-shell': hasDesktopShell, 'desktop-window-shell': isDesktopRuntime && showAppShell, 'sidebar-collapsed': sidebarCollapsed }"
  >
    <div
      v-if="isDesktopRuntime && showAppShell"
      class="desktop-window-drag-region"
      aria-hidden="true"
    ></div>

    <aside
      v-if="showAppShell"
      class="app-sidebar fixed bottom-0 left-0 top-0 z-40 hidden border-r border-[var(--border)] bg-[var(--sidebar-bg)] px-3 py-6 backdrop-blur-[10px] backdrop-saturate-110 lg:flex lg:flex-col"
    >
      <div class="sidebar-header relative mb-8 h-10">
        <button
          type="button"
          class="sidebar-brand absolute left-3 top-0 flex h-10 min-w-0 items-center gap-4 text-left"
          :tabindex="sidebarCollapsed ? -1 : 0"
          aria-label="WaveFlow 首页"
          @click="router.push('/radio')"
        >
          <span class="flex size-10 shrink-0 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-primary)]">
            <svg class="size-6" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m12 3 8 4.5-8 4.5-8-4.5L12 3Zm8 9-8 4.5L4 12m16 4.5L12 21l-8-4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </span>
          <span class="truncate text-base font-semibold tracking-normal">WaveFlow</span>
        </button>
      </div>

      <button
        type="button"
        class="sidebar-toggle flex size-10 shrink-0 items-center justify-center rounded-2xl border border-transparent text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]"
        :title="sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
        :aria-label="sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
        @click="toggleSidebar"
      >
        <svg
          class="size-6 transition-transform duration-300"
          :class="{ 'rotate-180': sidebarCollapsed }"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <path d="M15 6 9 12l6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <nav class="flex flex-1 flex-col gap-1">
        <button
          v-for="item in primaryNavItems"
          :key="item.label"
          type="button"
          class="nav-item flex h-14 items-center gap-4 rounded-2xl border px-3 text-base font-medium transition-all duration-300 ease-out"
          :class="[navItemClass(item), justActivatedLabel === item.label ? 'nav-item-just-activated' : '']"
          :title="sidebarCollapsed ? item.label : undefined"
          :aria-current="item.active ? 'page' : undefined"
          @click="activateNavItem(item)"
        >
          <svg v-if="item.icon === 'home'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m4 10 8-6 8 6v9a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1v-9Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
          <svg v-else-if="item.icon === 'tv'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="6" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="M9 3.5 12 6l3-2.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <svg v-else-if="item.icon === 'radio'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="2.2" fill="currentColor"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
          <svg v-else-if="item.icon === 'calendar'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="5.5" width="16" height="15" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="M8 3.5v4M16 3.5v4M4 10h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          <span class="nav-label truncate">{{ item.label }}</span>
        </button>

        <div class="my-4 h-px bg-[var(--border)]"></div>

        <button
          v-for="item in secondaryNavItems"
          :key="item.label"
          type="button"
          class="nav-item flex h-14 items-center gap-4 rounded-2xl border px-3 text-base font-medium transition-all duration-300 ease-out"
          :class="[navItemClass(item), justActivatedLabel === item.label ? 'nav-item-just-activated' : '']"
          :title="sidebarCollapsed ? item.label : undefined"
          :aria-current="item.active ? 'page' : undefined"
          @click="activateNavItem(item)"
        >
          <svg v-if="item.icon === 'layers'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 8.4 12 4l8 4.4-8 4.4L4 8.4Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M4 12.2 12 16.6l8-4.4M4 16l8 4.4L20 16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <svg v-else-if="item.icon === 'settings'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12.22 3h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V5a2 2 0 0 0-2-2Z" stroke="currentColor" stroke-width="1.65"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.65"/></svg>
          <svg v-else-if="item.icon === 'github'" class="size-6 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.5a9.5 9.5 0 0 0-3 18.51c.47.09.65-.2.65-.45v-1.62c-2.65.58-3.21-1.12-3.21-1.12-.43-1.1-1.06-1.39-1.06-1.39-.87-.59.07-.58.07-.58.96.07 1.47.99 1.47.99.85 1.47 2.23 1.05 2.77.8.09-.62.33-1.05.6-1.29-2.12-.24-4.35-1.06-4.35-4.72 0-1.04.37-1.89.99-2.56-.1-.24-.43-1.21.09-2.52 0 0 .81-.26 2.64.98A9.2 9.2 0 0 1 12 7.2c.82 0 1.64.14 2.4.35 1.83-1.24 2.64-.98 2.64-.98.52 1.31.19 2.28.09 2.52.62.67.99 1.52.99 2.56 0 3.67-2.23 4.48-4.36 4.72.34.29.64.85.64 1.72v2.55c0 .25.18.54.66.45A9.5 9.5 0 0 0 12 2.5Z"/></svg>
          <svg v-else class="size-6 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8"/><path d="M12 11.5v5M12 8h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          <span class="nav-label truncate">{{ item.label }}</span>
        </button>
      </nav>
    </aside>

    <div v-if="showAppShell" class="fixed inset-x-0 top-0 z-50 bg-[var(--bg)] lg:hidden">
      <div class="h-[env(safe-area-inset-top)]"></div>
      <div class="mobile-header-row flex h-12 items-center justify-between px-4 sm:px-6">
        <div class="mobile-mode-switch flex h-10 items-center rounded-full border border-[var(--border)] p-0.5 text-sm font-medium backdrop-blur-xl">
          <button
            type="button"
            class="flex h-9 items-center rounded-full px-3 transition-all sm:px-4"
            :class="activeMode === 'radio' ? 'bg-neutral-950 text-white dark:bg-white dark:text-black' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'"
            @click="router.push('/radio')"
          >
            Radio
          </button>
          <button
            type="button"
            class="flex h-9 items-center rounded-full px-3 transition-all sm:px-4"
            :class="activeMode === 'iptv' ? 'bg-neutral-950 text-white dark:bg-white dark:text-black' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'"
            @click="router.push('/tv')"
          >
            TV
          </button>
        </div>
        <div class="flex items-center gap-2">
          <div v-if="showGlobalSearch" class="mobile-search-shell relative flex items-center rounded-full backdrop-blur-xl transition-all duration-300 ease-out" :class="searchExpanded ? 'mobile-search-expanded' : 'mobile-search-collapsed'">
            <button type="button" class="mobile-search-trigger flex shrink-0 items-center justify-center text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)]" aria-label="搜索" @click="toggleSearch">
              <svg class="size-[19px] toolbar-search-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.7" /><path d="M16.5 16.5L21 21" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" /></svg>
            </button>
            <input ref="searchInputRef" v-model="searchQuery" type="text" placeholder="搜索频道、节目" class="h-full w-full bg-transparent pr-3 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]" :class="searchExpanded ? 'opacity-100' : 'pointer-events-none opacity-0'" @blur="onSearchBlur" />
          </div>
          <button type="button" class="mobile-action-btn" :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'" @click="toggleTheme">
            <svg v-if="isDark" class="size-[19px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v2M12 19v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M3 12h2M19 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.7"/></svg>
            <svg v-else class="size-[19px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20.5 14.4A7.7 7.7 0 0 1 9.6 3.5 8.5 8.5 0 1 0 20.5 14.4Z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <div ref="mobileMenuRef" class="relative">
            <button type="button" class="mobile-action-btn" aria-label="更多" :aria-expanded="mobileMenuOpen" aria-haspopup="menu" @click.stop="toggleMobileMenu">
              <svg class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="5" cy="12" r="1.35" fill="currentColor"/><circle cx="12" cy="12" r="1.35" fill="currentColor"/><circle cx="19" cy="12" r="1.35" fill="currentColor"/></svg>
            </button>
            <div v-if="mobileMenuOpen" class="mobile-nav-popover" role="menu" aria-label="更多导航">
              <div class="mobile-nav-popover__title">导航</div>
              <button
                v-for="item in mobileNavigationItems"
                :key="item.label"
                type="button"
                class="mobile-nav-popover__item"
                :class="{ 'mobile-nav-popover__item--active': item.active, 'mobile-nav-popover__item--disabled': item.disabled }"
                :aria-current="item.active ? 'page' : undefined"
                :aria-disabled="item.disabled ? 'true' : undefined"
                :disabled="item.disabled"
                role="menuitem"
                @click="navigateMobile(item)"
              >
                <span>{{ item.label }}</span>
                <span v-if="item.disabled" class="mobile-nav-popover__hint">暂不可用</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="showGlobalSearch" class="desktop-top-actions fixed top-6 z-30 hidden items-center gap-3 lg:flex">
      <div
        class="flex h-11 items-center overflow-hidden rounded-full border border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)] backdrop-blur-[12px] backdrop-saturate-110 transition-all duration-300 ease-out"
        :class="searchExpanded ? 'w-[260px] px-1' : 'w-11 px-0'"
      >
        <button
          type="button"
          class="flex size-11 shrink-0 items-center justify-center transition-colors hover:text-[var(--text-primary)]"
          aria-label="搜索"
          @click="toggleSearch"
        >
          <svg class="size-[19px] toolbar-search-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.7" /><path d="M16.5 16.5L21 21" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" /></svg>
        </button>
        <input
          ref="desktopSearchInputRef"
          v-model="searchQuery"
          type="text"
          placeholder="搜索频道、节目"
          class="min-w-0 flex-1 bg-transparent pr-3 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]"
          :class="searchExpanded ? 'opacity-100' : 'pointer-events-none opacity-0'"
          @blur="onSearchBlur"
        />
      </div>

      <button type="button" class="desktop-action-btn" :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'" @click="toggleTheme">
        <svg v-if="isDark" class="size-[19px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v2M12 19v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M3 12h2M19 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.7"/></svg>
        <svg v-else class="size-[19px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20.5 14.4A7.7 7.7 0 0 1 9.6 3.5 8.5 8.5 0 1 0 20.5 14.4Z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>

    <div class="app-main">
      <router-view />
    </div>

    <BottomPlayer v-if="showAppShell" />

    <AudioEngine v-if="showAppShell" />

    <FullPlayer v-if="showAppShell" />

    <ToastHost />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, provide, ref, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { usePlayerStore } from './stores/player'
import BottomPlayer from './components/BottomPlayer.vue'
import AudioEngine from './components/AudioEngine.vue'
import FullPlayer from './components/FullPlayer.vue'
import ToastHost from './components/ToastHost.vue'

const THEME_STORAGE_KEY = 'waveflow-theme'
const isDark = ref(false)
let mediaQuery = null

const scrollRef = ref(null)
provide('scrollRef', scrollRef)

const playerStore = usePlayerStore()
const { activeMode } = storeToRefs(playerStore)
const router = useRouter()
const route = useRoute()
const mobileMenuOpen = ref(false)
const mobileMenuRef = ref(null)
const showAppShell = computed(() => !route.meta.authPage)
const showGlobalSearch = computed(() => showAppShell.value && route.path !== '/market')
const hasDesktopShell = computed(() => showAppShell.value)
const isDesktopRuntime = Boolean(window.WAVEFLOW_DESKTOP?.platform)
const SIDEBAR_STORAGE_KEY = 'waveflow-sidebar-collapsed'
const sidebarCollapsed = ref(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1')
const justActivatedLabel = ref('')
watch(sidebarCollapsed, (value) => {
  window.localStorage.setItem(SIDEBAR_STORAGE_KEY, value ? '1' : '0')
})
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
}
const primaryNavItems = computed(() => [
  { label: '电视', icon: 'tv', route: '/tv', active: route.path === '/tv' },
  { label: '电台', icon: 'radio', route: '/radio', active: route.path === '/radio' },
])
const secondaryNavItems = computed(() => [
  { label: 'Market', icon: 'layers', route: '/market', active: route.path === '/market' },
  { label: '设置', icon: 'settings', route: '/settings/sources', active: route.path.startsWith('/settings') },
  { label: 'GitHub', icon: 'github', external: 'https://github.com/amplace/waveflow', active: false },
])
const THEME_CHROME_COLORS = {
  light: '#F6F7F8',
  dark: '#090A0B',
}
const THEME_STATUS_BAR = {
  light: 'default',
  dark: 'default',
}

watch(() => route.path, (path) => {
  mobileMenuOpen.value = false
  playerStore.setActiveMode(path.startsWith('/tv') || path.startsWith('/iptv') || path.startsWith('/admin') || path.startsWith('/settings') || path.startsWith('/market') ? 'iptv' : 'radio')
}, { immediate: true })

watch(showAppShell, (visible) => {
  if (!visible) playerStore.stopAndClearPlayback()
}, { immediate: true, flush: 'sync' })

function forceMetaContent(name, content) {
  const old = document.querySelector(`meta[name="${name}"]`)
  if (old) {
    if (old.content !== content) old.content = content
    return old
  }
  const meta = document.createElement('meta')
  meta.name = name
  meta.content = content
  document.head.appendChild(meta)
  return meta
}

// 动态修改 Safari / PWA 浏览器 chrome 颜色
function updateThemeColor(isDarkMode = document.documentElement.classList.contains('dark'), source = '', options = {}) {
  const themeColor = isDarkMode ? THEME_CHROME_COLORS.dark : THEME_CHROME_COLORS.light
  const statusBarStyle = isDarkMode ? THEME_STATUS_BAR.dark : THEME_STATUS_BAR.light
  const colorScheme = isDarkMode ? 'dark' : 'light'
  const refreshChrome = options.refreshChrome !== false

  console.log('[theme-sync]', {
    source,
    inputIsDark: isDarkMode,
    htmlDark: document.documentElement.classList.contains('dark'),
    bodyDark: document.body.classList.contains('dark'),
    prefersDark: window.matchMedia('(prefers-color-scheme: dark)').matches,
    themeColor,
    colorScheme,
    metaBefore: [...document.querySelectorAll('meta[name="theme-color"]')].map(m => m.outerHTML),
  })

  forceMetaContent('theme-color', themeColor)
  forceMetaContent('apple-mobile-web-app-status-bar-style', statusBarStyle)
  forceMetaContent('color-scheme', colorScheme)

  console.log('[theme-sync-after]', {
    themeMetas: [...document.querySelectorAll('meta[name="theme-color"]')].map(m => m.outerHTML),
    colorSchemeMetas: [...document.querySelectorAll('meta[name="color-scheme"]')].map(m => m.outerHTML),
    htmlBg: getComputedStyle(document.documentElement).backgroundColor,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    appBg: getComputedStyle(document.getElementById('app')).backgroundColor,
  })

  document.documentElement.style.setProperty('background-color', themeColor, 'important')
  document.documentElement.style.setProperty('color-scheme', colorScheme, 'important')
  document.documentElement.style.setProperty('--waveflow-page-bg', themeColor, 'important')
  if (document.body) {
    document.body.style.setProperty('background-color', themeColor, 'important')
    document.body.style.setProperty('color-scheme', colorScheme, 'important')
  }
  const appEl = document.getElementById('app')
  appEl?.style.setProperty('background-color', themeColor, 'important')
  appEl?.style.setProperty('color-scheme', colorScheme, 'important')

  window.dispatchEvent(new CustomEvent('waveflow-theme-chrome-sync', {
    detail: { isDarkMode, themeColor, refreshChrome, source },
  }))
}

window.__waveflowSyncThemeChrome = updateThemeColor

function applyTheme(source = 'applyTheme', options = {}) {
  const savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY)
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches

  const shouldUseDark = savedTheme ? savedTheme === 'dark' : prefersDark

  isDark.value = shouldUseDark
  document.documentElement.classList.toggle('dark', shouldUseDark)
  
  updateThemeColor(shouldUseDark, `${source}:${savedTheme ? 'manual' : 'system'}`, options)
}

applyTheme()

const searchQuery = ref('')
provide('searchQuery', searchQuery)

const searchExpanded = ref(false)
const searchInputRef = ref(null)
const desktopSearchInputRef = ref(null)

const mobileNavigationItems = computed(() => [
  ...(!['/radio', '/tv'].includes(route.path)
    ? [{ label: '返回内容', route: activeMode.value === 'radio' ? '/radio' : '/tv', active: false }]
    : []),
  { label: 'Market', route: '/market', active: route.path === '/market' },
  { label: '设置', route: '/settings/sources', active: route.path.startsWith('/settings') },
])

function navItemClass(item) {
  if (item.active) {
    return 'border-[var(--border-strong)] bg-[var(--surface-active)] text-[var(--text-primary)]'
  }
  return 'border-transparent text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]'
}

function activateNavItem(item) {
  if (item.external) {
    window.open(item.external, '_blank', 'noopener,noreferrer')
    return
  }
  if (!item.route) return
  justActivatedLabel.value = item.label
  setTimeout(() => { justActivatedLabel.value = '' }, 350)
  router.push(item.route)
}

function toggleSearch() {
  searchExpanded.value = !searchExpanded.value
  if (searchExpanded.value) {
    nextTick(() => {
      const isMobileViewport = window.matchMedia('(max-width: 1023px)').matches
      (isMobileViewport ? searchInputRef : desktopSearchInputRef).value?.focus()
    })
  } else {
    searchQuery.value = ''
  }
}

function onSearchBlur() {
  if (!searchQuery.value.trim()) {
    searchExpanded.value = false
  }
}

function toggleMobileMenu() {
  mobileMenuOpen.value = !mobileMenuOpen.value
}

function navigateMobile(item) {
  if (item.disabled || !item.route) return
  mobileMenuOpen.value = false
  router.push(item.route)
}

function handleMobileMenuPointerDown(event) {
  if (mobileMenuOpen.value && !mobileMenuRef.value?.contains(event.target)) {
    mobileMenuOpen.value = false
  }
}

function handleMobileMenuKeydown(event) {
  if (event.key === 'Escape') {
    mobileMenuOpen.value = false
  }
}

function toggleTheme() {
  const nextTheme = isDark.value ? 'light' : 'dark'
  window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme)
  applyTheme('toggleTheme')
}

function handleSystemThemeChange(e) {
  window.localStorage.removeItem(THEME_STORAGE_KEY)
  applyTheme('systemThemeChange')
}

function handleVisibilityChange() {
  if (document.visibilityState === 'visible') {
    applyTheme('visibilitychange', { refreshChrome: false })
  }
}

onMounted(() => {
  // 挂载后只需要绑定监听器，不需要再调 applyTheme 了
  mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  mediaQuery.addEventListener('change', handleSystemThemeChange)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  document.addEventListener('pointerdown', handleMobileMenuPointerDown)
  document.addEventListener('keydown', handleMobileMenuKeydown)
})

onBeforeUnmount(() => {
  if (mediaQuery) {
    mediaQuery.removeEventListener('change', handleSystemThemeChange)
  }
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  document.removeEventListener('pointerdown', handleMobileMenuPointerDown)
  document.removeEventListener('keydown', handleMobileMenuKeydown)
})
</script>
