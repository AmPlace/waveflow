<template>
  <main
    class="iptv-main page-shell min-h-screen w-full page-with-mini-player"
    :class="{ 'iptv-density-compact': densityMode === 'compact' }"
  >
    <header class="mb-7 space-y-7">
      <div class="lg:pr-[300px]">
        <TagFilterRow
          :items="categoryTabs"
          :is-active="isSelectedCategory"
          @select="selectCategoryTab"
        />
      </div>

      <div class="flex items-center justify-between gap-4">
        <div class="flex min-w-0 items-center gap-3">
          <span class="iptv-live-section-icon inline-flex size-6 shrink-0 items-center justify-center text-[var(--text-secondary)]">
            <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M5 12a7 7 0 0 1 14 0M2.5 12a9.5 9.5 0 0 1 19 0M9 12a3 3 0 0 1 6 0" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
          </span>
          <h1 class="hidden truncate text-lg font-semibold leading-none text-[var(--text-primary)] lg:block">正在直播</h1>
          <span class="shrink-0 text-sm font-medium text-[var(--text-secondary)]">
            共 {{ filteredChannels.length }} 个频道
          </span>
          <span v-if="loading" class="hidden text-sm text-[var(--text-tertiary)] sm:inline">加载中...</span>
        </div>

        <div class="flex shrink-0 items-center gap-2">
          <button
            type="button"
            class="iptv-density-toggle"
            :class="{ 'iptv-density-toggle--standard': densityMode === 'standard' }"
            :aria-pressed="densityMode === 'standard'"
            :aria-label="densityMode === 'standard' ? '仅显示 Logo' : '显示频道信息'"
            :title="densityMode === 'standard' ? '切换为仅显示 Logo' : '切换为显示频道信息'"
            data-density-toggle
            @click="toggleDensityMode"
          >
            <svg v-if="densityMode === 'standard'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <path d="M4 7h10M4 17h16M14 7l2-2m-2 2 2 2M10 17l2-2m-2 2 2 2" />
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
              <rect x="4" y="4" width="6" height="6" rx="1" />
              <rect x="14" y="4" width="6" height="6" rx="1" />
              <rect x="4" y="14" width="6" height="6" rx="1" />
              <rect x="14" y="14" width="6" height="6" rx="1" />
            </svg>
            <span class="sr-only">{{ densityMode === 'standard' ? '切换为仅显示 Logo' : '切换为显示频道信息' }}</span>
          </button>

          <button
            type="button"
            class="iptv-sort-trigger inline-flex h-10 shrink-0 items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]"
            @click="nextSortMode"
          >
            <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="16" y2="12"/><line x1="4" y1="18" x2="12" y2="18"/>
            </svg>
            {{ currentSortLabel }}
          </button>
        </div>
      </div>
    </header>

    <div
      v-if="catalogNotice"
      class="iptv-catalog-notice mb-5 flex min-h-10 items-center justify-between gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm text-[var(--text-secondary)]"
      role="status"
      aria-live="polite"
    >
      <span class="min-w-0 truncate">{{ catalogNotice }}</span>
      <button
        v-if="!loading"
        type="button"
        class="shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
        data-iptv-catalog-retry
        @click="loadChannels"
      >
        重试
      </button>
    </div>

    <section
      ref="gridRef"
      class="relative min-h-[240px] w-full"
      :style="{ height: `${totalHeight}px` }"
      :aria-busy="loading ? 'true' : 'false'"
      data-iptv-catalog-grid
    >
      <div v-if="filteredChannels.length === 0" class="flex min-h-[240px] items-center justify-center px-4 py-12">
        <div class="flex max-w-md flex-col items-center gap-3 text-center" role="status" aria-live="polite" data-iptv-catalog-empty-state>
          <span class="flex size-11 items-center justify-center rounded-xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)]">
            <svg v-if="loading && allChannels.length === 0" class="size-5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8" stroke-opacity=".25"/><path d="M20 12a8 8 0 0 0-8-8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            <svg v-else-if="catalogState === 'error' && allChannels.length === 0" class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 4 21 20H3L12 4Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 9v5M12 17h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            <svg v-else class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.7"/><path d="M8.5 12h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
          </span>
          <p v-if="loading && allChannels.length === 0" class="text-sm text-[var(--text-secondary)]">正在加载频道目录...</p>
          <template v-else-if="catalogState === 'error' && allChannels.length === 0">
            <p class="text-sm text-[var(--text-secondary)]">频道目录暂时无法加载</p>
            <button
              type="button"
              class="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
              data-iptv-catalog-retry
              @click="loadChannels"
            >
              重试加载
            </button>
          </template>
          <p v-else-if="catalogState === 'empty' && !hasActiveFilter" class="text-sm text-[var(--text-secondary)]">暂无可用频道</p>
          <template v-else>
            <p class="text-sm text-[var(--text-secondary)]">没有匹配的频道</p>
            <button
              type="button"
              class="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
              @click="clearCatalogFilters"
            >
              清除筛选
            </button>
          </template>
        </div>
      </div>

      <template v-else>
      <div
        v-for="row in virtualRows"
        :key="row.startIndex"
        class="absolute left-0 top-0 w-full"
        :style="{ transform: `translateY(${row.startIndex * (rowHeight + gap)}px)` }"
      >
        <div
          class="grid"
          :style="{ gap: 'var(--card-gap)', gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }"
        >
          <button
            v-for="item in row.items"
            :key="item.channel.logical_channel_id || item.channel.canonical_key || item.channel.name"
            type="button"
            :aria-label="`播放 ${item.channel.name}`"
            :style="{ height: `${cardHeight}px` }"
            :disabled="isUnavailable(item.channel)"
            :data-canonical-key="item.channel.canonical_key"
            class="channel-card group relative overflow-hidden rounded-[var(--card-radius)] border border-[var(--border)] bg-[var(--card-bg)] text-left outline-none transition duration-200 ease-out hover:-translate-y-0.5 hover:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-45"
            :class="[defaultCoverClass(item.channel), { 'channel-card-current': isCurrentChannel(item.channel) }]"
            @click="playChannel(item.channel)"
          >
            <span class="channel-card__logo-card-visual" aria-hidden="true">
              <span
                class="channel-card__logo-stage"
                :class="logoStageClass(item.channel)"
              >
                <img
                  v-if="shouldShowChannelLogo(item.channel)"
                  class="channel-card__center-logo"
                  :class="logoImageClass(item.channel)"
                  :src="channelLogoUrl(item.channel)"
                  alt=""
                  loading="lazy"
                  decoding="async"
                  @load="classifyChannelLogo(item.channel, $event)"
                  @error="markChannelLogoFailed(item.channel)"
                />
                <span
                  v-else
                  class="channel-card__text-logo"
                  :class="textLogoSizeClass(item.channel)"
                  :title="channelDisplayName(item.channel)"
                >
                  {{ channelDisplayName(item.channel) }}
                </span>
              </span>
              <span class="channel-card__logo-card-shade"></span>
            </span>
            <span v-if="densityMode === 'standard'" class="card-info">
              <span class="card-channel">
                <span
                  class="channel-play-state-dot"
                  :class="channelStatusDotClass(item.channel)"
                  :title="channelStatusLabel(item.channel)"
                  :aria-label="channelStatusLabel(item.channel)"
                  role="img"
                />
                <span class="card-channel-name">{{ item.channel.name }}</span>
              </span>
              <span class="card-program-name">{{ cardSubtitle(item.channel) }}</span>
            </span>
            <span
              v-else-if="isCompactStatusVisible(item.channel)"
              class="channel-card__compact-status channel-play-state-dot"
              :class="channelStatusDotClass(item.channel)"
              :title="channelStatusLabel(item.channel)"
              :aria-label="channelStatusLabel(item.channel)"
              role="img"
            />
          </button>
        </div>
      </div>
      </template>
    </section>
  </main>
</template>

<script setup>
import { computed, inject, nextTick, onBeforeUnmount, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useScroll, useThrottleFn } from '@vueuse/core'
import { usePlayerStore } from '../stores/player'
import { useToastStore } from '../stores/toast'
import { fetchAggregatedChannels, fetchChannelVisual } from '../api/iptv'
import { useEpg } from '../composables/useEpg'
import { useLogoVisual } from '../composables/useLogoVisual'
import { loadVisual, abortPendingVisualRequests } from '../composables/visualLoader'
import TagFilterRow from '../components/TagFilterRow.vue'
import { channelIdentity, isChannelAllNotLive, isChannelAllUnsupported, isChannelAllUrlsBlocked } from '../utils/sourceIdentity'
import { IPTV_CHANNEL_SORT_MODES, sortIptvChannels } from '../utils/iptvChannelList'
import {
  IPTV_CARD_DENSITIES,
  defaultIptvCardDensity,
  readIptvCardDensityPreference,
  writeIptvCardDensityPreference,
} from '../utils/iptvCardDensity'
import { heightForWidth, IPTV_CARD_RATIOS } from '../utils/iptvCardGeometry'
import { epgBatchRefreshDelay } from '../utils/epgViewing'
import { iptvCardProgrammeTitle } from '../utils/iptvViewing'
import { channelVisualCandidates } from '../utils/channelVisual'

const playerStore = usePlayerStore()
const toastStore = useToastStore()

const scrollRef = inject('scrollRef')
const searchQuery = inject('searchQuery')

const allChannels = ref([])
const allGroups = ref([])
const selectedGroup = ref('')
const loading = ref(false)
const catalogState = ref('loading')
const epgMap = ref({})
const { batchCurrent } = useEpg()

let requestSeq = 0
let activeRequestSeq = 0
let activeController = null
let epgBatchSeq = 0
let epgBatchController = null
let epgBatchRefreshTimer = null

function _clearEpgBatchRefreshTimer() {
  if (!epgBatchRefreshTimer) return
  clearTimeout(epgBatchRefreshTimer)
  epgBatchRefreshTimer = null
}

function _invalidateEpgBatchRefresh() {
  epgBatchSeq += 1
  _clearEpgBatchRefreshTimer()
  if (epgBatchController) {
    epgBatchController.abort()
    epgBatchController = null
  }
}

function _invalidateListRequest() {
  requestSeq += 1
  activeRequestSeq = 0
  _invalidateEpgBatchRefresh()
  if (activeController) {
    activeController.abort()
    activeController = null
  }
}

function _isCurrentListRequest(seq) {
  return seq === activeRequestSeq && seq === requestSeq
}

function _scheduleEpgBatchRefresh(listSeq, keys, map) {
  _clearEpgBatchRefreshTimer()
  if (!_isCurrentListRequest(listSeq) || !keys.length) return
  const delay = epgBatchRefreshDelay(map)
  epgBatchRefreshTimer = setTimeout(() => {
    epgBatchRefreshTimer = null
    if (_isCurrentListRequest(listSeq)) void _refreshBatchCurrent(listSeq, keys)
  }, delay)
}

async function _refreshBatchCurrent(listSeq, keys) {
  if (!_isCurrentListRequest(listSeq) || !keys.length) return { applied: false }
  const batchSeq = ++epgBatchSeq
  epgBatchController?.abort()
  const ctrl = new AbortController()
  epgBatchController = ctrl
  const map = await batchCurrent(keys, { signal: ctrl.signal })
  if (
    batchSeq !== epgBatchSeq
    || !_isCurrentListRequest(listSeq)
    || ctrl.signal.aborted
  ) {
    return { applied: false }
  }
  if (epgBatchController === ctrl) epgBatchController = null
  if (map) epgMap.value = map
  _scheduleEpgBatchRefresh(listSeq, keys, map || epgMap.value)
  return { applied: Boolean(map) }
}
const logoCandidateIndexes = ref({})
const channelSortMode = computed(() => playerStore.iptvChannelSortMode)
const categoryTabs = computed(() => ['全部', ...allGroups.value])
const hasActiveFilter = computed(() => Boolean(selectedGroup.value || searchQuery.value.trim()))
const catalogNotice = computed(() => {
  if (loading.value && allChannels.value.length > 0) return '正在更新频道目录，当前频道仍可使用'
  if (catalogState.value === 'stale') return '频道目录更新失败，已保留上次频道'
  if (catalogState.value === 'error' && allChannels.value.length > 0) return '频道目录暂时无法更新，已保留现有频道'
  return ''
})

// Core owns source-scoped visual metadata TTL/cache.  The Home keeps only
// the current projection needed to render each card; it does not key a
// second long-lived cache by canonical channel.
const visualMetadata = ref({})

function triggerVisualForChannel(ch) {
  const key = ch?.canonical_key || ''
  if (!key) return
  if (visualMetadata.value[key]) return
  const seq = activeRequestSeq
  loadVisual(key, (signal) => fetchChannelVisual(key, { signal })).then(entry => {
    // A lazy visual request may outlive a search/group refresh.  It must not
    // project an old source result into the new list, even if the browser
    // fetch implementation does not honor AbortController immediately.
    if (!_isCurrentListRequest(seq)) return
    if (entry?.stable_cover_url || entry?.avatar_url || entry?.dynamic_cover_url || entry?.cover_url) {
      visualMetadata.value = { ...visualMetadata.value, [key]: entry }
    }
  }).catch(() => {})
}

const {
  displayName: channelDisplayName,
  shouldShowLogo: shouldShowChannelLogo,
  logoStageClass,
  logoImageClass,
  classifyLogo: classifyChannelLogo,
  markLogoFailed: markChannelLogoFailed,
  textLogoSizeClass,
} = useLogoVisual({
  getLogoUrl: channelLogoUrl,
  getDisplayName: channelDisplayNameValue,
  getIdentityKey: channelLogoIdentityKey,
  getFailureKey: channelLogoCandidateKey,
  getVisualKey: (ch) => `${channelLogoIdentityKey(ch)}|${channelLogoUrl(ch)}`,
  enableWide: true,
  onBeforeFail: advanceChannelLogoCandidate,
  fallbackName: '未知频道',
})

function nextSortMode() {
  const idx = IPTV_CHANNEL_SORT_MODES.findIndex(m => m.key === channelSortMode.value)
  playerStore.setIptvChannelSortMode(IPTV_CHANNEL_SORT_MODES[(idx + 1) % IPTV_CHANNEL_SORT_MODES.length].key)
}

const currentSortLabel = computed(() => IPTV_CHANNEL_SORT_MODES.find(m => m.key === channelSortMode.value)?.label || '默认排序')

function selectCategoryTab(tab) {
  abortPendingVisualRequests()
  selectedGroup.value = tab === '全部' ? '' : tab
  loadChannels()
}

function clearCatalogFilters() {
  const hadSearch = Boolean(searchQuery.value.trim())
  selectedGroup.value = ''
  searchQuery.value = ''
  if (!hadSearch) loadChannels()
}

function isSelectedCategory(tab) {
  return tab === '全部' ? !selectedGroup.value : selectedGroup.value === tab
}

async function loadChannels() {
  _invalidateListRequest()
  abortPendingVisualRequests()
  epgMap.value = {}
  const seq = ++requestSeq
  activeRequestSeq = seq
  const ctrl = new AbortController()
  activeController = ctrl

  loading.value = true
  try {
    const group = selectedGroup.value
    const search = searchQuery.value.trim()
    const data = await fetchAggregatedChannels({ group, search, signal: ctrl.signal })
    if (!_isCurrentListRequest(seq)) return { applied: false }
    if (!data || typeof data !== 'object' || !Array.isArray(data.channels)) {
      throw new Error('频道目录响应无效')
    }
    allChannels.value = data.channels
    visualMetadata.value = {}
    if (_visualObserver) _visualObserver.disconnect()
    _visualObservedKeys.clear()
    playerStore.refreshIptvChannelContext({
      group,
      search,
      channels: allChannels.value,
    })
    if (!group && !search) {
      allGroups.value = Array.isArray(data.groups) ? data.groups : []
    }
    catalogState.value = allChannels.value.length ? 'success' : 'empty'
    const keys = allChannels.value.map(c => c.canonical_key).filter(Boolean)
    if (keys.length && _isCurrentListRequest(seq)) {
      void _refreshBatchCurrent(seq, keys)
    }
    // 延迟触发视觉元数据加载：等 DOM 更新后，IntersectionObserver 开始观察可见卡片
    await nextTick()
    if (!_isCurrentListRequest(seq)) return { applied: false }
    _observeVisibleCards()
    return { applied: true }
  } catch (e) {
    if (!_isCurrentListRequest(seq)) return { applied: false }
    const cancelled = e?.name === 'AbortError' || e?.message === '请求已取消'
    if (cancelled) return { applied: false }
    console.error('加载频道失败:', e?.status ? `HTTP ${e.status}` : e?.name || 'request_failed')
    catalogState.value = allChannels.value.length ? 'stale' : 'error'
    return { applied: false, error: e }
  } finally {
    if (_isCurrentListRequest(seq)) loading.value = false
  }
}

// ── IntersectionObserver：仅加载视口附近频道的视觉元数据 ──
let _visualObserver = null
let _visualObservedKeys = new Set()
let _observeTimer = null

function _setupVisualObserver() {
  if (_visualObserver) _visualObserver.disconnect()
  _visualObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const key = entry.target.dataset.canonicalKey
          if (key) {
            _visualObserver.unobserve(entry.target)
            _visualObservedKeys.delete(key)
            triggerVisualForChannel({ canonical_key: key })
          }
        }
      }
    },
    { rootMargin: '300px' }  // 提前 300px 加载
  )
}

function _observeVisibleCards() {
  if (!_visualObserver) _setupVisualObserver()
  // 查找所有已渲染但未观察的 channel card 元素
  const cards = document.querySelectorAll('.channel-card[data-canonical-key]')
  for (const card of cards) {
    const key = card.dataset.canonicalKey
    if (key && !_visualObservedKeys.has(key)) {
      _visualObservedKeys.add(key)
      _visualObserver.observe(card)
    }
  }
}

function _scheduleObserveCards() {
  if (_observeTimer) return
  _observeTimer = setTimeout(() => {
    _observeTimer = null
    _observeVisibleCards()
  }, 200)  // 滚动期间最多每 200ms 扫描一次
}

onMounted(() => {
  _setupVisualObserver()
})

onUnmounted(() => {
  _invalidateListRequest()
  abortPendingVisualRequests()
  if (_visualObserver) {
    _visualObserver.disconnect()
    _visualObserver = null
  }
  _visualObservedKeys.clear()
  if (_observeTimer) {
    clearTimeout(_observeTimer)
    _observeTimer = null
  }
})

const filteredChannels = computed(() => {
  return sortIptvChannels(allChannels.value, channelSortMode.value)
})

onMounted(loadChannels)

watch(searchQuery, () => { loadChannels() })

function isCurrentChannel(ch) {
  const current = playerStore.pendingIptvChannel || playerStore.currentIptvChannel
  return Boolean(current && channelIdentity(current) === channelIdentity(ch))
}

function isUntested(ch) {
  return ch.urls.every(u => {
    const status = u.probe_status || ''
    return status ? status === 'untested' : u.is_working === -1
  })
}

function isAllFailed(ch) {
  return isChannelAllUrlsBlocked(ch)
}

function isAllNotLive(ch) {
  return isChannelAllNotLive(ch)
}

function isUnavailable(ch) {
  // 测速结果只影响排序和提示；明确禁用或全部 unsupported 才禁止点击。
  return isChannelAllUrlsBlocked(ch) || isChannelAllUnsupported(ch)
}

function isAnyPlayable(ch) {
  return ch.urls?.some(u => {
    const status = u.probe_status || ''
    if (status) return status === 'online'
    return u.is_working === 1
  })
}

function cardSubtitle(ch) {
  const current = epgMap.value[ch.canonical_key]?.current
  return iptvCardProgrammeTitle(current, ch.group_name || '')
}

function channelStatusKind(ch) {
  if (isAnyPlayable(ch)) return 'live'
  if (isAllNotLive(ch)) return 'warn'
  if (isUntested(ch)) return 'neutral'
  if (isAllFailed(ch) || isUnavailable(ch)) return 'danger'
  return 'neutral'
}

function channelStatusDotClass(ch) {
  return `channel-play-state-dot--${channelStatusKind(ch)}`
}

function channelStatusLabel(ch) {
  const kind = channelStatusKind(ch)
  if (kind === 'warn') return '未开播'
  if (kind === 'danger') return '不可用'
  if (kind === 'neutral') return '未测试'
  return '可播放'
}

function isCompactStatusVisible(ch) {
  const kind = channelStatusKind(ch)
  return kind === 'warn' || kind === 'danger'
}

function defaultCoverClass() {
  return 'channel-card--logo-card'
}

function channelLogoUrl(ch) {
  const candidates = channelLogoCandidates(ch)
  if (!candidates.length) return ''
  const index = logoCandidateIndexes.value[channelLogoCandidateKey(ch)] || 0
  return candidates[index] || candidates[0] || ''
}

function channelLogoCandidates(ch) {
  const visual = visualMetadata.value[ch?.canonical_key || '']
  return channelVisualCandidates(ch?.logo_url, visual)
}

function channelLogoIdentityKey(ch) {
  return ch?.canonical_key || ch?.tvg_id || ch?.tvg_name || ch?.name || ''
}

function channelLogoCandidateKey(ch) {
  return `${channelLogoIdentityKey(ch)}|${channelLogoCandidates(ch).join('|')}`
}

function channelDisplayNameValue(ch) {
  return String(ch?.name || '未知频道').trim() || '未知频道'
}

function advanceChannelLogoCandidate(ch) {
  const candidates = channelLogoCandidates(ch)
  const key = channelLogoCandidateKey(ch)
  const index = logoCandidateIndexes.value[key] || 0
  if (index < candidates.length - 1) {
    logoCandidateIndexes.value = {
      ...logoCandidateIndexes.value,
      [key]: index + 1,
    }
    return true
  }
  return false
}

async function playChannel(ch) {
  if (isUnavailable(ch)) return
  if (!ch.urls || !ch.urls.length) return
  // not_live 放开可点：上次测速时没开播，不代表现在没播。照样尝试播放，
  // 但给用户一个轻提示，避免"点了没反应"的困惑。不阻塞、不 return。
  if (isAllNotLive(ch)) {
    toastStore.info('该频道上次检测未开播，正在尝试播放')
  }
  await playerStore.playIptvChannel(ch, {
    progressive: true,
    channelContext: {
      origin: 'iptv-home',
      group: selectedGroup.value,
      search: searchQuery.value.trim(),
      channels: allChannels.value,
    },
  })
}

const gridRef = ref(null)
const containerWidth = ref(1024)
const viewportWidth = ref(typeof window === 'undefined' ? 1280 : window.innerWidth)
const densityPreference = ref(readIptvCardDensityPreference())
const densityMode = computed(() => densityPreference.value || defaultIptvCardDensity(viewportWidth.value))
let resizeObserver = null

function setDensityMode(value) {
  if (value !== IPTV_CARD_DENSITIES.STANDARD && value !== IPTV_CARD_DENSITIES.COMPACT) return
  densityPreference.value = value
  writeIptvCardDensityPreference(value)
}

function toggleDensityMode() {
  setDensityMode(
    densityMode.value === IPTV_CARD_DENSITIES.STANDARD
      ? IPTV_CARD_DENSITIES.COMPACT
      : IPTV_CARD_DENSITIES.STANDARD,
  )
}

const { y: scrollY } = useScroll(scrollRef)
const throttledScrollY = useThrottleFn((val) => { scrollPosition.value = val }, 16)
const scrollPosition = ref(0)
watchEffect(() => { throttledScrollY(scrollY.value) })

// 滚动时虚拟列表渲染新卡片 → 重新 observe（IntersectionObserver）
watch(scrollPosition, () => { _scheduleObserveCards() })

const gap = computed(() => {
  if (densityMode.value !== IPTV_CARD_DENSITIES.COMPACT) return 20
  return viewportWidth.value < 1024 ? 14 : 18
})
const cardFooterHeight = computed(() => densityMode.value === IPTV_CARD_DENSITIES.STANDARD ? 44 : 0)
const cardWidthHeightRatio = computed(() => (
  densityMode.value === IPTV_CARD_DENSITIES.COMPACT
    ? IPTV_CARD_RATIOS.COMPACT
    : IPTV_CARD_RATIOS.STANDARD_VISUAL
))

const columns = computed(() => {
  const w = viewportWidth.value
  if (w >= 1280) return 5
  if (w >= 1024) return 4
  return 2
})

const rowHeight = computed(() => {
  const cols = columns.value
  const cardWidth = (containerWidth.value - gap.value * (cols - 1)) / cols
  return heightForWidth(cardWidth, cardWidthHeightRatio.value, cardFooterHeight.value)
})

const cardHeight = computed(() => rowHeight.value)

const rows = computed(() => {
  const cols = columns.value
  const channels = filteredChannels.value
  const result = []
  for (let i = 0; i < channels.length; i += cols) {
    result.push(channels.slice(i, i + cols).map((channel) => ({
      channel,
    })))
  }
  return result
})

const virtualRows = computed(() => {
  const totalRows = rows.value.length
  if (totalRows === 0) return []
  const rh = rowHeight.value + gap.value
  const startRow = Math.max(0, Math.floor(scrollPosition.value / rh) - 3)
  const viewportH = scrollRef.value?.clientHeight || window.innerHeight
  const endRow = Math.min(totalRows, Math.ceil((scrollPosition.value + viewportH) / rh) + 3)
  return rows.value.slice(startRow, endRow).map((items, i) => ({
    items,
    startIndex: startRow + i,
  }))
})

const totalHeight = computed(() => rows.value.length * (rowHeight.value + gap.value))

function updateViewportWidth() {
  viewportWidth.value = window.innerWidth
}

onMounted(() => {
  resizeObserver = new ResizeObserver((entries) => {
    for (const entry of entries) {
      containerWidth.value = entry.contentRect.width
      viewportWidth.value = window.innerWidth
    }
  })
  if (gridRef.value) resizeObserver.observe(gridRef.value)
  window.addEventListener('resize', updateViewportWidth)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewportWidth)
  if (resizeObserver) resizeObserver.disconnect()
})
</script>
